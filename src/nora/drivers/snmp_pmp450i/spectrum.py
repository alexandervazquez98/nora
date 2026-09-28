"""Spectrum sweep + maintenance-window guard — issue #62 WU-3.

The real Cambium WHISP-BOX-MIBV2-MIB spectrum-sweep protocol is
SET/GET-poll, NOT the synthetic GET-only loop the shipped slice-4 code
exercised. The synthetic noise-floor scalars (``.221.1/.2/.3``) do not
exist on physical firmware; the real protocol uses
``whispBoxSpectrumScanDuration`` (``.220.0``, SET duration in seconds)
and ``whispBoxSpectrumScanAction`` (``.221.0``, SET ``8`` arm + SET
``1`` start, GET-poll until ``0`` idle).

The slice-4 helper :func:`fetch_spectrum` now:

1. Reads :class:`Settings.nora_maintenance_window_*` and enforces the
   configured window boundary — calls outside the window raise
   :class:`MaintenanceWindowViolation` BEFORE any wire frame.
2. Resolves the OID catalog for the device's ``(vendor, model,
   firmware)`` triple (the same minor-mismatch fallback applies).
3. Opens a :class:`WritableSnmpClient` via
   ``driver._writable_client_factory`` (Driver-R2 carve-out).
4. SETs the duration, then SETs the action ``8`` (arm) and ``1`` (start).
5. GET-polls ``.221.0`` every ``nora_spectrum_sweep_poll_interval_seconds``
   until the scalar returns ONE OF the completion sentinels in
   :data:`_SWEEP_COMPLETION_STATUSES` (``{0, 3, 4}``) OR
   ``nora_spectrum_sweep_timeout_seconds`` elapses. The actual GET-idle
   sentinels on physical Cambium WHISP-BOX-MIBV2-MIB firmware 25.0.1
   are ``3`` (``idleNoSpectrumAnalysis`` — sweep ran, no results
   available) and ``4`` (``idleCompleteSpectrumAnalysis`` — sweep ran,
   results available); ``0`` is the ``stopSpectrumAnalysis`` SET
   command but some agents return it by GET post-completion, so it is
   accepted defensively. ``5`` is
   ``inProgressTimedSpectrumAnalysis`` (still sweeping).
6. On completion: returns a typed :class:`SpectrumSweepResult` with
   ``scan_outcome="COMPLETED"``, ``final_status`` echoing the LAST
   polled completion value (typically ``4`` on real hardware), and
   empty ``ranked_clean_frequencies`` / ``noise_floor_dbm`` (real
   per-bin noise decoding is a future slice).
7. On timeout: raises :class:`SpectrumSweepTimeout` carrying the
   ``device_id``, ``duration_seconds``, and ``last_status`` (the last
   polled value, typically ``5`` while still in progress, or ``-1``
   when nothing was polled) so the orchestrator can see WHERE the
   sweep got stuck.

Operator-observed AP sweep timing (firmware 25.0.1): the AP initiates
a sector-coordinated sweep that takes ~95-105s regardless of the
``duration_seconds`` parameter SET on ``.220.0`` — the SET value is
NOT the wall-clock duration. SM sweeps complete in ~15s and
re-associate in ~20s. The default
``nora_spectrum_sweep_timeout_seconds=150`` covers both AP and SM
paths with headroom.

Tier-1 ``operator_confirmed`` gate FIRST (preserved from slice 4);
the real protocol emits SET frames against the spectrum-scan scalars
but is bounded (duration + arm + start, no frequency change) and
recovers to idle without side effects.

PR #66 review follow-up #2 adds the production-critical wire-error swallow
to ``_poll_sweep_status`` (issue #62, deployed after ``6a59546``): during an
active sweep the radio goes off-channel and ``client.get_oid`` raises
``SnmpTimeoutError`` / ``NetworkUnreachableError`` (subclasses of
``DriverError``). The poll loop catches these as transient — ``last_status = -1``
and keeps polling — so a single mid-sweep packet loss does not abort the
entire helper.

PR #66 review follow-up #3 (this commit, 2026-09-19) replaces the
1.0s startup-guard floor with a sweep-duration-scaled guard. After
re-deploying ``a607eb9`` to physical PMP 450i hardware the operator
found the 1.0s minimum guard insufficient: the radio's SNMP agent
takes 1.5-2.5s to flush TDD buffers and engage sweep mode, so at
t=1.087s (our 1.0s guard satisfied) the radio was STILL returning
its pre-sweep status 4, and the helper reported false-positive
completion while the sweep had not yet started. The new guard is
``elapsed >= float(sweep_duration_seconds)`` — "a timed sweep cannot
physically complete before the requested duration" — with no
minimum floor. The ``sweep_duration_seconds`` SET value is the
authoritative minimum the radio itself enforces, so guarding on it
keeps the poll loop honest regardless of firmware revision.
``DriverError`` swallow from follow-up #2 continues to handle the
temporary RF link silence during the sweep.

Zero-Leakage: only TEST-NET-1 (``192.0.2.x``) host literals; no real
IPs, hostnames, serials, or credentials.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from nora.drivers.exceptions import (
    DriverError,
    MaintenanceWindowViolation,
    SpectrumHttpFetchError,
    SpectrumSweepTimeout,
    SpectrumXmlParseError,
    Tier1ClearanceRequired,
)
from nora.drivers.snmp_pmp450i.client import SnmpClient, WritableSnmpClient
from nora.drivers.snmp_pmp450i.spectrum_http import (
    SpectrumBin,
    fetch_spectrum_xml,
    noise_floor_per_channel,
    parse_spectrum_xml,
    rank_clean_frequencies,
)

if TYPE_CHECKING:
    from nora.config import Settings


logger = logging.getLogger("nora.drivers.snmp_pmp450i.spectrum")


# ---------------------------------------------------------------------------
# Typed result + outcome literal — issue #62 / WU-3
# ---------------------------------------------------------------------------


ScanOutcome = Literal["COMPLETED", "TIMEOUT", "ABORTED"]


# WHISP-BOX-MIBV2-MIB `whispBoxSpectrumScanAction` (.221.0) GET-side
# completion sentinels. Per the official MIB + empirical hardware
# verification on firmware 25.0.1:
#   - 0  stopSpectrumAnalysis              (SET-only abort command;
#                                          some agents return 0 by GET
#                                          post-completion — accepted
#                                          defensively)
#   - 3  idleNoSpectrumAnalysis            (GET; idle, no results)
#   - 4  idleCompleteSpectrumAnalysis      (GET; idle, results ready)
# Values NOT in this set are NOT a completion (notably 5
# `inProgressTimedSpectrumAnalysis` indicates an active sweep). The
# helper accepts the WHOLE set defensively because firmware revisions
# differ in which GET-idle value they emit; the operator's
# physical-hardware observation is that 4 is the success sentinel on
# 25.0.1 but 3 also occurs.
_SWEEP_COMPLETION_STATUSES: frozenset[int] = frozenset({0, 3, 4})


class SpectrumSweepResult(BaseModel):
    """Typed spectrum sweep result — issue #62 WU-3.

    Replaces the legacy `SpectrumAnalysis` (built around synthetic
    noise-floor scalars that physical Cambium PMP 450i firmware does
    not implement). The real Cambium WHISP-BOX-MIBV2-MIB sweep
    protocol is SET + GET-poll; the typed result carries the sweep
    metadata + timeline so the operator can reason about the run.

    Fields:
      - device_id: the inventory device the sweep ran against.
      - scan_started_at: UTC ISO-8601 timestamp marking the SET-arm call.
      - scan_completed_at: UTC ISO-8601 timestamp marking the moment
        .221.0 returned a completion sentinel from
        `_SWEEP_COMPLETION_STATUSES`. On TIMEOUT this is the timeout
        instant; on ABORTED this is the abort instant.
      - sweep_duration_seconds: the duration value SET on .220.0
        (echoes the operator's request so the audit trail is self-contained).
      - final_status: the last value read from .221.0 (typically 4 on
        a real-hardware COMPLETED sweep, 0/3 in firmware revisions
        that emit the alternative idle sentinels; the last polled
        value on TIMEOUT — e.g. 5 in-progress, or -1 if nothing was
        polled before the wire failed).
      - scan_outcome: COMPLETED | TIMEOUT | ABORTED.
      - ranked_clean_frequencies: empty list in WU-3 (real per-bin
        noise decoding is a future slice); kept as a field so the
        schema is stable for that follow-up; populated by WU-2 on
        a sweep that completed with ``final_status == 4``.
      - noise_floor_dbm: empty dict in WU-3 (same reason); populated
        by WU-2 on a sweep that completed with ``final_status == 4``.
      - post_sweep_error: empty string on a clean sweep; one-line
        diagnostic on a sweep that completed with ``final_status == 4``
        but the post-sweep HTTP fetch + XML parse ladder failed
        non-fatally (the sweep outcome stays COMPLETED — only the
        bin decode could not be performed).
    """

    model_config = ConfigDict(frozen=True)

    device_id: str
    scan_started_at: str
    scan_completed_at: str
    sweep_duration_seconds: int
    final_status: int
    scan_outcome: ScanOutcome
    ranked_clean_frequencies: list[float] = Field(default_factory=list)
    noise_floor_dbm: dict[str, float] = Field(default_factory=dict)
    # Issue #70 / WU-2: non-fatal error from the post-sweep HTTP fetch
    # + XML parse ladder. Empty string when the ladder succeeded OR
    # when the sweep sentinel was 0 / 3 (defensive / no-results — the
    # ladder is skipped). Populated with a one-line diagnostic when
    # the ladder ran but failed (network unreachable, XML malformed,
    # or one or more SM hosts failed). The sweep outcome itself is
    # still COMPLETED — the operator sees the sweep ran but the bin
    # decode could not be performed.
    post_sweep_error: str = ""


# ---------------------------------------------------------------------------
# Internal helpers — issue #62 / WU-3
# ---------------------------------------------------------------------------


def _is_inside_maintenance_window(
    *,
    now: datetime,
    window_minutes: int,
    start_minutes_ago: int,
) -> bool:
    """True when ``now`` falls inside the configured maintenance window.

    The window runs from ``(now - start_minutes_ago)`` for
    ``window_minutes`` minutes. ``window_minutes == 0`` short-circuits
    to ``True`` — no window is enforced (the default). The
    boundary is closed on the lower end, open on the upper end.
    """
    if window_minutes <= 0:
        return True
    window_start = now - timedelta(minutes=start_minutes_ago)
    window_end = window_start + timedelta(minutes=window_minutes)
    return window_start <= now < window_end


def _arm_sweep(
    *,
    client: WritableSnmpClient,
    duration_oid: str,
    action_oid: str,
    duration_seconds: int,
) -> None:
    """SET duration → SET 8 (arm) → SET 1 (start). No GET yet.

    Three SET frames in sequence; any wire failure raises the
    underlying puresnmp exception (callers wrap them in typed driver
    exceptions at the seam).
    """
    client.set(duration_oid, duration_seconds)
    client.set(action_oid, 8)  # arm
    client.set(action_oid, 1)  # start


def _poll_sweep_status(
    *,
    client: SnmpClient,  # read-only — .221.0 GET only
    action_oid: str,
    poll_interval_seconds: float,
    timeout_seconds: int,
    sweep_duration_seconds: int,
) -> int:
    """GET-poll .221.0 until one of the completion sentinels or timeout.

    Two production-critical guards:

    1. **Sweep-duration-scaled guard** (PR #66 review follow-up #3):
       completion sentinels are only accepted after
       ``elapsed >= float(sweep_duration_seconds)`` seconds have
       elapsed since the SET. A timed sweep cannot physically
       complete before the requested duration. The guard
       ``elapsed >= sweep_duration_seconds`` is the operator's
       recommended replacement for the 1.0s floor that was
       insufficient for the TDD buffer flush latency observed on
       physical hardware (1.5-2.5s on firmware 25.0.1).

    2. **Wire-error swallow** (PR #66 review follow-up #2): during an
       active sweep the radio goes off-channel and ``client.get_oid``
       raises ``SnmpTimeoutError`` / ``NetworkUnreachableError``
       (subclasses of ``DriverError``). We treat these as transient —
       ``last_status = -1`` and keep polling — so a single mid-sweep
       packet loss does not abort the entire helper.

    Completion sentinels (WHISP-BOX-MIBV2-MIB::whispBoxSpectrumScanAction):
      - 0 stopSpectrumAnalysis (SET-only; some agents return 0 by GET
        post-completion — accept defensively)
      - 3 idleNoSpectrumAnalysis (GET; idle, no results available)
      - 4 idleCompleteSpectrumAnalysis (GET; idle, results available)

    Returns the LAST polled value (could be a completion sentinel or an
    in-progress sentinel like 5). Callers check membership in
    `_SWEEP_COMPLETION_STATUSES` to decide success vs. timeout.
    """
    deadline = time.monotonic() + timeout_seconds
    start = time.monotonic()
    last_status = -1  # sentinel: never polled
    while time.monotonic() < deadline:
        try:
            last_status = int(client.get_oid(action_oid))
        except (KeyError, ValueError, TypeError):
            last_status = -1
        except DriverError:
            # Radio off-channel during sweep — transient.
            last_status = -1
        elapsed = time.monotonic() - start
        if elapsed >= float(sweep_duration_seconds) and last_status in _SWEEP_COMPLETION_STATUSES:
            return last_status
        time.sleep(poll_interval_seconds)
    return last_status


def _fetch_and_populate_post_sweep(
    *,
    ap_host: str,
    sm_hosts: list[str] | None,
    settings: "Settings | None",
    band_range: tuple[float, float] | None = None,
) -> tuple[list[float], dict[str, float], str]:
    """Run the post-sweep HTTP ladder and populate the RF bin fields.

    Ladder (issue #70):

      1. GET ``http://{ap_host}/SpectrumAnalysis.xml`` with bounded
         retry / timeout (``nora_spectrum_http_*`` Settings).
      2. If ``sm_hosts`` is non-empty, sleep
         ``nora_spectrum_sm_reassociation_timeout_seconds`` (the SMs
         need to re-associate after the sector-coordinated sweep),
         then GET each SM's XML.
      3. Parse every payload via :func:`parse_spectrum_xml`; aggregate
         the bins across AP + SMs.
      4. Compute :func:`noise_floor_per_channel` (per-channel
         worst-leg avg_dbm) and :func:`rank_clean_frequencies`
         (top-N worst-case-min-first). Issue #81: when
         ``band_range`` is supplied, :func:`rank_clean_frequencies`
         filters out-of-band bins BEFORE ranking so the 3 GHz
         radio (C030045A002A) does not surface out-of-band 4 GHz
         "cleanest" candidates.

    Failure policy: ANY exception raised by the HTTP fetch ladder OR
    the XML parser is NON-FATAL — the helper returns empty lists /
    dicts and a one-line diagnostic string. The sweep itself is
    already COMPLETED; the operator sees the sweep ran fine but the
    bin decode could not be performed, and the diagnostic points at
    the first failure (network unreachable, XML malformed, etc.).

    Returns ``(ranked_clean_frequencies, noise_floor_dbm, post_sweep_error)``.
    """
    if settings is None:
        http_timeout_seconds = 10.0
        http_max_retries = 5
        http_retry_delay_seconds = 3.0
        sm_reassoc_seconds = 15.0
        ranking_top_n = 10
    else:
        http_timeout_seconds = float(getattr(settings, "nora_spectrum_http_timeout_seconds", 10.0))
        http_max_retries = int(getattr(settings, "nora_spectrum_http_max_retries", 5))
        http_retry_delay_seconds = float(
            getattr(settings, "nora_spectrum_http_retry_delay_seconds", 3.0)
        )
        sm_reassoc_seconds = float(
            getattr(settings, "nora_spectrum_sm_reassociation_timeout_seconds", 15.0)
        )
        ranking_top_n = int(getattr(settings, "nora_spectrum_ranking_top_n", 10))

    ap_error: str = ""
    ap_xml: str | None = None
    try:
        ap_xml = fetch_spectrum_xml(
            ap_host,
            timeout_seconds=http_timeout_seconds,
            max_retries=http_max_retries,
            retry_delay_seconds=http_retry_delay_seconds,
        )
    except (SpectrumHttpFetchError, SpectrumXmlParseError) as exc:
        ap_error = f"ap={ap_host!r}: {exc}"
        logger.warning(
            "post-sweep AP XML fetch failed; "
            "sweep outcome stays COMPLETED with empty bin fields: %s",
            ap_error,
        )

    if ap_xml is None or ap_error:
        return ([], {}, ap_error)

    try:
        ap_bins = parse_spectrum_xml(ap_xml)
    except SpectrumXmlParseError as exc:
        err = f"ap={ap_host!r} parse: {exc}"
        logger.warning("post-sweep AP XML parse failed: %s", err)
        return ([], {}, err)

    all_bins: list[SpectrumBin] = list(ap_bins)
    sm_error: str = ""

    if sm_hosts:
        # Sleep once for SM re-association. Bounded to >= 0 so a
        # misconfigured negative Settings value cannot corrupt the
        # helper. The bound check is also enforced in
        # _validate_spectrum_http_settings.
        sleep_seconds = max(0.0, float(sm_reassoc_seconds))
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

        for sm_host in sm_hosts:
            if not sm_host:
                continue
            try:
                sm_xml = fetch_spectrum_xml(
                    sm_host,
                    timeout_seconds=http_timeout_seconds,
                    max_retries=http_max_retries,
                    retry_delay_seconds=http_retry_delay_seconds,
                )
                sm_bins = parse_spectrum_xml(sm_xml)
            except (SpectrumHttpFetchError, SpectrumXmlParseError) as exc:
                sm_error = (
                    f"sm={sm_host!r}: {exc}"
                    if not sm_error
                    else f"{sm_error}; sm={sm_host!r}: {exc}"
                )
                logger.warning(
                    "post-sweep SM XML fetch/parse failed (continuing with partial data): %s",
                    exc,
                )
                continue
            all_bins.extend(sm_bins)

    if not all_bins:
        return ([], {}, "")

    noise = noise_floor_per_channel(all_bins)
    ranked = rank_clean_frequencies(all_bins, top_n=ranking_top_n, band_range=band_range)
    return (ranked, noise, sm_error)


def _read_band_range_after_sweep(
    *,
    client: Any,
    catalog: Any,
) -> tuple[float, float] | None:
    """Read the radio's ``radioFrequencyBand`` OID after a successful sweep.

    Issue #81. Maps the integer returned by the OID to a band-class
    name via :func:`nora.drivers.snmp_pmp450i.band_plan._band_name_from_enum`
    and then to a numeric range via
    :func:`nora.drivers.snmp_pmp450i.band_plan._range_for_band`. The
    resulting range is passed to :func:`rank_clean_frequencies` so
    out-of-band bins are dropped before ranking.

    Args:
        client: The writable SNMP client that just completed the
            sweep. Caller MUST NOT close it before this returns.
        catalog: The resolved OID catalog (used to look up the
            ``radioFrequencyBand`` OID string).

    Returns:
        ``(low_mhz, high_mhz)`` when the radio's band identity is
        recognised AND maps to a known regulatory range. ``None``
        when ANY of the following is true (preserves v1 behaviour
        of trusting the spectrum analyser data as ground truth):

        * The catalog does not carry a ``radioFrequencyBand`` OID
          (legacy catalog, pre-v2).
        * The GET raises (race, timeout, agent error).
        * The integer value is not in the recognised Cambium enum
          table (``unknown`` sentinel, future firmware).
        * The mapped band name does not have a regulatory range
          (shouldn't happen with current data).

    Every failure path emits a structured ``logger.warning`` so an
    operator scanning the post-sweep logs can see WHY the band
    filter was skipped. Failures are NEVER fatal — the sweep
    already completed and the operator still gets the bin data.
    """
    from nora.drivers.snmp_pmp450i.band_plan import (  # local import — avoid circular
        _band_name_from_enum,
        _range_for_band,
    )

    band_oid = catalog.oids.get("radioFrequencyBand")
    if not band_oid:
        logger.warning(
            "post-sweep: catalog lacks radioFrequencyBand OID; "
            "rank_clean_frequencies will use no band filter (v1 behaviour)."
        )
        return None
    try:
        raw = client.get_oid(band_oid)
    except Exception as exc:  # noqa: BLE001 — defensive: any agent / wire failure
        logger.warning(
            "post-sweep: radioFrequencyBand GET failed; "
            "rank_clean_frequencies will use no band filter. error=%r",
            exc,
        )
        return None
    band_name = _band_name_from_enum(raw)
    if band_name is None:
        logger.warning(
            "post-sweep: radioFrequencyBand returned unrecognised value %r; "
            "rank_clean_frequencies will use no band filter.",
            raw,
        )
        return None
    band_range = _range_for_band(band_name)
    if band_range is None:
        logger.warning(
            "post-sweep: radioFrequencyBand mapped to %r but no regulatory "
            "range is modelled; rank_clean_frequencies will use no band filter.",
            band_name,
        )
        return None
    logger.info(
        "post-sweep: radio band=%s range=(%.1f, %.1f) MHz — ranker filtered.",
        band_name,
        band_range[0],
        band_range[1],
    )
    return band_range


# ---------------------------------------------------------------------------
# Public helpers — issue #62 / WU-3
# ---------------------------------------------------------------------------


def fetch_spectrum(
    *,
    driver: Any,
    device_id: str,
    settings: "Settings | None" = None,
    operator_confirmed: bool = False,
    sweep_duration_seconds: int | None = None,
    sm_hosts: list[str] | None = None,
) -> SpectrumSweepResult:
    """Run the real Cambium sweep: SET duration → SET 8 → SET 1 → GET-poll until completion.

    Issue #70 / WU-2: after a successful sweep (final_status in
    ``{3, 4}``), the helper runs a post-sweep HTTP fetch ladder:

      1. ``final_status == 4`` only — the ladder is skipped for
         ``0`` (defensive abort) and ``3`` (idle, no results).
      2. AP XML: ``fetch_spectrum_xml(host)`` with bounded retry /
         timeout (``Settings.nora_spectrum_http_*``).
      3. ``sm_hosts``: if non-None and non-empty, sleep
         ``Settings.nora_spectrum_sm_reassociation_timeout_seconds``
         (the SMs need to re-associate), then sequentially fetch
         each SM's XML.
      4. Parse every XML via ``parse_spectrum_xml``; aggregate bins.
      5. Compute ``noise_floor_dbm`` + ``ranked_clean_frequencies``.
      6. Any HTTP / parse failure is NON-FATAL — the result carries
         a one-line diagnostic in ``post_sweep_error`` and the sweep
         outcome stays COMPLETED.

    Parameters (added in WU-2):
      * ``sm_hosts``: list of SM IPv4 literals (TEST-NET-1 / RFC 5737
        only at the inventory layer). ``None`` (default) skips the SM
        ladder — only the AP XML is fetched. Empty list also skips.

    Gates (preserve existing behavior):
      1. Tier-1 operator_confirmed gate FIRST (raises Tier1ClearanceRequired).
      2. Maintenance window check SECOND (raises MaintenanceWindowViolation).
      3. Catalog resolution (raises LookupError on missing OID names).
      4. Open a WritableSnmpClient via driver._writable_client_factory.
      5. SET duration → SET 8 → SET 1.
      6. GET-poll .221.0 every poll_interval_seconds until one of the
         completion sentinels in `_SWEEP_COMPLETION_STATUSES`
         (``{0, 3, 4}``) or timeout elapses. PR #66 review
         follow-up #3: completion sentinels are only accepted after
         ``elapsed >= float(sweep_duration_seconds)`` seconds have
         elapsed since the SET (a timed sweep cannot physically
         complete before the requested duration — replaces the 1.0s
         floor that was insufficient for the 1.5-2.5s TDD buffer flush
         latency on physical PMP 450i firmware 25.0.1). PR #66 review
         follow-up #2: mid-sweep wire errors
         (``SnmpTimeoutError`` / ``NetworkUnreachableError``,
         subclasses of ``DriverError``) are swallowed as transient.
      7. On timeout: raise SpectrumSweepTimeout.
      8. On success: return SpectrumSweepResult with COMPLETED outcome,
         final_status echoing the actual polled completion value
         (typically 4 on real-hardware firmware 25.0.1), and empty
         ranked_clean_frequencies (real per-bin noise decoding is a
         future slice).

    Completion sentinel semantics (WHISP-BOX-MIBV2-MIB::
    whispBoxSpectrumScanAction, per official MIB + operator's
    physical-hardware verification on firmware 25.0.1):
      - 0 stopSpectrumAnalysis (SET-only abort; some agents return 0
        by GET post-completion — accepted defensively)
      - 3 idleNoSpectrumAnalysis (GET; idle, no results available)
      - 4 idleCompleteSpectrumAnalysis (GET; idle, results available)
      - 5 inProgressTimedSpectrumAnalysis (GET; sweep still running)

    Operator-observed AP sweep timing: ~95-105s regardless of the
    ``duration_seconds`` parameter (the AP initiates a sector-coordinated
    sweep). SM sweeps complete in ~15s and re-associate in ~20s. The
    default ``nora_spectrum_sweep_timeout_seconds=150`` covers both
    paths with headroom. The Settings knob is the only way to override
    the poll-loop timeout — ``sweep_duration_seconds`` controls the
    ``.220.0`` SET value, NOT the poll-loop timeout.

    Per issue #43 ADDED requirement "Tier-1 Operator Clearance Gate":
    ``operator_confirmed`` defaults to ``False`` (fail-closed). The
    server-side gate raises :class:`Tier1ClearanceRequired` BEFORE any
    wire frame when ``operator_confirmed`` is False (or absent). The
    LLM orchestrator MUST request operator clearance before invoking.
    """
    # 1. Tier-1 gate FIRST — fires before any wire frame and before the
    # maintenance-window check (highest-priority invariant).
    if not operator_confirmed:
        raise Tier1ClearanceRequired(
            tool="snmp_run_spectrum_analysis",
            message=(
                "Tier-1 spectrum sweep requires operator_confirmed=True; "
                "default False (and absent-parameter) refuses the call"
            ),
        )

    device = driver._inventory.get(device_id)  # noqa: SLF001 — internal API
    now = datetime.now(timezone.utc)

    # Settings resolution (same pattern as the legacy code).
    if settings is None:
        settings = getattr(driver, "_runtime_settings", None)
    if settings is None:
        window_minutes = 0
        start_minutes_ago = 0
        duration_default = 15
        poll_interval_default = 1.0
        timeout_default = 60
    else:
        window_minutes = int(getattr(settings, "nora_maintenance_window_minutes", 0))
        start_minutes_ago = int(getattr(settings, "nora_maintenance_window_start_minutes_ago", 0))
        duration_default = int(getattr(settings, "nora_spectrum_sweep_duration_seconds", 15))
        poll_interval_default = float(
            getattr(settings, "nora_spectrum_sweep_poll_interval_seconds", 1.0)
        )
        timeout_default = int(getattr(settings, "nora_spectrum_sweep_timeout_seconds", 60))

    # Maintenance window (unchanged).
    if not _is_inside_maintenance_window(
        now=now,
        window_minutes=window_minutes,
        start_minutes_ago=start_minutes_ago,
    ):
        raise MaintenanceWindowViolation(
            f"spectrum sweep refused: now={now.isoformat()} "
            f"outside maintenance window "
            f"(window_minutes={window_minutes}, start_minutes_ago={start_minutes_ago})"
        )

    catalog = driver._catalog_registry.resolve(  # noqa: SLF001 — internal API
        (device.vendor, device.model, device.firmware)
    )
    if "spectrumScanDuration" not in catalog.oids:
        raise LookupError(
            f"spectrum OID 'spectrumScanDuration' missing from catalog "
            f"(vendor={catalog.vendor}, model={catalog.model}, firmware={catalog.firmware})"
        )
    if "spectrumScanAction" not in catalog.oids:
        raise LookupError(
            f"spectrum OID 'spectrumScanAction' missing from catalog "
            f"(vendor={catalog.vendor}, model={catalog.model}, firmware={catalog.firmware})"
        )
    duration_oid = catalog.oids["spectrumScanDuration"]
    action_oid = catalog.oids["spectrumScanAction"]

    effective_duration = (
        int(sweep_duration_seconds) if sweep_duration_seconds is not None else duration_default
    )

    client = driver._writable_client_factory(device)  # noqa: SLF001 — internal API
    band_range: tuple[float, float] | None = None
    try:
        _arm_sweep(
            client=client,
            duration_oid=duration_oid,
            action_oid=action_oid,
            duration_seconds=effective_duration,
        )
        last_status = _poll_sweep_status(
            client=client,
            action_oid=action_oid,
            poll_interval_seconds=poll_interval_default,
            timeout_seconds=timeout_default,
            sweep_duration_seconds=effective_duration,
        )
        # Issue #81: after the sweep completes (and only when the
        # sweep produced usable results, i.e. final_status == 4),
        # read the radio's ``radioFrequencyBand`` OID so the
        # post-sweep ladder can filter the ranker to the radio's
        # actual regulatory band. This is the band-limit fix for
        # 3 GHz Cambium hardware (C030045A002A) whose hardware
        # spectrum sweep measures up to ~4200 MHz and returns
        # artificial -99 dBm floor readings above 3900 MHz.
        if last_status == 4:
            band_range = _read_band_range_after_sweep(client=client, catalog=catalog)
    finally:
        try:
            client.close()
        except Exception:  # pragma: no cover - close is best-effort
            pass

    completed_at = datetime.now(timezone.utc)
    if last_status not in _SWEEP_COMPLETION_STATUSES:
        raise SpectrumSweepTimeout(
            device_id=str(getattr(device, "host", device_id)),
            duration_seconds=effective_duration,
            last_status=last_status,
        )

    # Issue #70 / WU-2: post-sweep HTTP ladder runs ONLY when the
    # sweep produced usable results. Sentinel 0 (defensive abort)
    # and 3 (idle, no results) skip the ladder; only sentinel 4
    # (idle, results available) triggers the fetch.
    ranked: list[float] = []
    noise: dict[str, float] = {}
    post_sweep_error = ""
    if last_status == 4:
        ap_host = str(getattr(device, "host", device_id))
        ranked, noise, post_sweep_error = _fetch_and_populate_post_sweep(
            ap_host=ap_host,
            sm_hosts=sm_hosts,
            settings=settings,
            band_range=band_range,
        )

    return SpectrumSweepResult(
        device_id=str(getattr(device, "host", device_id)),
        scan_started_at=now.isoformat(),
        scan_completed_at=completed_at.isoformat(),
        sweep_duration_seconds=effective_duration,
        final_status=last_status,  # now actually 0/3/4, not always 0
        scan_outcome="COMPLETED",
        ranked_clean_frequencies=ranked,
        noise_floor_dbm=noise,
        post_sweep_error=post_sweep_error,
    )


__all__ = [
    "SpectrumSweepResult",
    "SpectrumSweepTimeout",
    "fetch_spectrum",
    "ScanOutcome",
    "_arm_sweep",
    "_poll_sweep_status",
    "_fetch_and_populate_post_sweep",
    "_SWEEP_COMPLETION_STATUSES",
]
