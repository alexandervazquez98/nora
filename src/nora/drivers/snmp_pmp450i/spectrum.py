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
    MaintenanceWindowViolation,
    SpectrumSweepTimeout,
    Tier1ClearanceRequired,
)
from nora.drivers.snmp_pmp450i.client import SnmpClient, WritableSnmpClient

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
        schema is stable for that follow-up.
      - noise_floor_dbm: empty dict in WU-3 (same reason).
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
) -> int:
    """GET-poll .221.0 until one of the completion sentinels or timeout.

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
    last_status = -1  # sentinel: never polled
    while time.monotonic() < deadline:
        try:
            last_status = int(client.get_oid(action_oid))
        except (KeyError, ValueError, TypeError):
            last_status = -1
        if last_status in _SWEEP_COMPLETION_STATUSES:
            return last_status
        time.sleep(poll_interval_seconds)
    return last_status


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
) -> SpectrumSweepResult:
    """Run the real Cambium sweep: SET duration → SET 8 → SET 1 → GET-poll until completion.

    Gates (preserve existing behavior):
      1. Tier-1 operator_confirmed gate FIRST (raises Tier1ClearanceRequired).
      2. Maintenance window check SECOND (raises MaintenanceWindowViolation).
      3. Catalog resolution (raises LookupError on missing OID names).
      4. Open a WritableSnmpClient via driver._writable_client_factory.
      5. SET duration → SET 8 → SET 1.
      6. GET-poll .221.0 every poll_interval_seconds until one of the
         completion sentinels in `_SWEEP_COMPLETION_STATUSES`
         (``{0, 3, 4}``) or timeout elapses.
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
        )
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

    return SpectrumSweepResult(
        device_id=str(getattr(device, "host", device_id)),
        scan_started_at=now.isoformat(),
        scan_completed_at=completed_at.isoformat(),
        sweep_duration_seconds=effective_duration,
        final_status=last_status,  # now actually 0/3/4, not always 0
        scan_outcome="COMPLETED",
        ranked_clean_frequencies=[],
        noise_floor_dbm={},
    )


__all__ = [
    "SpectrumSweepResult",
    "SpectrumSweepTimeout",
    "fetch_spectrum",
    "ScanOutcome",
    "_arm_sweep",
    "_poll_sweep_status",
    "_SWEEP_COMPLETION_STATUSES",
]
