"""Spectrum sweep + maintenance-window guard — slice 4 (PR 4 commit 2).

The slice-4 spectrum-analysis tool (``snmp_run_spectrum_analysis``)
delegates the wire path through :func :func:`fetch_spectrum`, which:

1. Reads :class:`Settings.nora_maintenance_window_*` and enforces the
   configured window boundary — calls outside the window raise
   :class:`MaintenanceWindowViolation` BEFORE any SNMP frame is
   emitted.
2. Resolves the OID catalog for the device's ``(vendor, model,
   firmware)`` triple (the same minor-mismatch fallback applies).
3. Walks the three spectrum-noise-floor OIDs and folds the response
   into a typed :class:`SpectrumAnalysis`.

The candidate list is sorted by **ascending** noise floor (lowest
noise = cleanest carrier) and capped to ``top_n`` (default 3). The
sweep is read-only — the tool never issues an SNMP SET frame, so the
slice-4 HITL gate is NOT consulted on the spectrum path.

Zero-Leakage: only TEST-NET-1 (``192.0.2.x``) host literals; no real
IPs, hostnames, serials, or credentials.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from nora.drivers.exceptions import MaintenanceWindowViolation, Tier1ClearanceRequired

if TYPE_CHECKING:
    from nora.config import Settings
    from nora.drivers.oid_catalog import OidCatalog


logger = logging.getLogger("nora.drivers.snmp_pmp450i.spectrum")


# ---------------------------------------------------------------------------
# Spectrum constants — the three noise-floor OIDs are swept on every
# call; the candidate list is keyed off the indices embedded in the
# ``spectrumChannelRank`` OID. The ``MHZ_BASE_FREQS`` tuple pins the
# candidate set; new frequencies are added when the catalog
# publishes additional OIDs (slice 4 ships three).
# ---------------------------------------------------------------------------

_MHZ_TO_KHZ: int = 1000

# The three noise-floor OID names the slice-4 catalog ships.
SPECTRUM_NOISE_FLOOR_OID_NAMES: tuple[str, ...] = (
    "spectrumNoiseFloorA",
    "spectrumNoiseFloorB",
    "spectrumNoiseFloorC",
)

# The candidate frequencies (MHz) the sweep emits. Index-aligned with
# ``SPECTRUM_NOISE_FLOOR_OID_NAMES``: index 0 ↔ ``A``, index 1 ↔ ``B``,
# index 2 ↔ ``C``. Slice 4 pins three candidates; the catalog can
# extend the set in a future slice.
_MHZ_BASE_FREQS: tuple[int, ...] = (5780, 5800, 5820)

# Default cap on the ranked clean-frequencies list. The tool never
# returns more than ``top_n`` candidates.
DEFAULT_TOP_N: int = 3


# ---------------------------------------------------------------------------
# Typed models — slice 4 spectrum surface.
# ---------------------------------------------------------------------------


class SpectrumAnalysis(BaseModel):
    """Typed spectrum sweep result — slice 4 read tool return.

    Fields:

    * ``ranked_clean_frequencies`` — list of carrier frequencies
      (kHz), sorted by ascending noise floor (lowest noise =
      cleanest first). Capped to ``top_n`` (default 3).
    * ``noise_floor_dbm`` — mapping from frequency (kHz, as
      ``float``) to noise floor (dBm). Tolerant of unknown keys
      (the catalog may ship fewer OIDs than the helper assumes).
    * ``scan_started_at`` — UTC ISO-8601 timestamp marking the
      moment the sweep opened its SNMP session.
    * ``device_id`` — the inventory device the sweep ran against.
    """

    model_config = ConfigDict(frozen=True)

    device_id: str
    ranked_clean_frequencies: list[float] = Field(default_factory=list)
    noise_floor_dbm: dict[str, float] = Field(default_factory=dict)
    scan_started_at: str


# ---------------------------------------------------------------------------
# Internal helpers
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


def _resolve_spectrum_oids(catalog: "OidCatalog") -> dict[str, str]:
    """Return the dotted OIDs for the three spectrum-noise-floor OIDs.

    Returns a name -> dotted-oid dict so the spectrum helper can
    look up individual values by name. Missing names raise
    :class:`LookupError` (the catalog verification gate rejects
    catalogs missing any of them).
    """
    dotted: dict[str, str] = {}
    for name in SPECTRUM_NOISE_FLOOR_OID_NAMES:
        if name not in catalog.oids:
            raise LookupError(
                f"spectrum OID {name!r} missing from catalog "
                f"(vendor={catalog.vendor}, model={catalog.model}, firmware={catalog.firmware})"
            )
        dotted[name] = catalog.oids[name]
    return dotted


def _coerce_int(value: str | int | None) -> int:
    """Coerce ``value`` to ``int``; missing values collapse to ``0``.

    Cambium agents return OctetString for some scalars; the fold
    tolerates both via ``int(value)``; unparseable strings collapse
    to ``0`` so the rank computation still reaches a stable
    ordering.
    """
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Public helpers — slice 4 spectrum tool bodies.
# ---------------------------------------------------------------------------


def fetch_spectrum(
    *,
    driver: Any,
    device_id: str,
    settings: "Settings | None" = None,
    operator_confirmed: bool = False,
) -> SpectrumAnalysis:
    """Read the spectrum sweep via ``Pmp450iSnmpDriver``.

    Tier-1 operator-clearance gate FIRST: the helper raises
    :class:`Tier1ClearanceRequired` if ``operator_confirmed`` is False
    (the default, including the absent-parameter case) BEFORE any wire
    frame is emitted. The maintenance-window check follows on success
    (a confirmed clearance does NOT bypass the window). The catalog
    resolution and the noise-floor sweep follow on success.

    Per `pmp450i-radio-tools/spec.md` ADDED requirement "Tier-1
    Operator Clearance Gate" and "snmp_run_spectrum_analysis Operator
    Clearance Gate": Tier-1 tools may be actively disruptive, so the
    server-side gate enforces explicit operator clearance.

    The ranked clean-frequencies list is sorted by ascending noise
    floor (lowest noise = cleanest first) and capped to
    :data:`DEFAULT_TOP_N` entries.
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

    if settings is None:
        settings = getattr(driver, "_runtime_settings", None)
    if settings is None:
        window_minutes = 0
        start_minutes_ago = 0
    else:
        window_minutes = int(getattr(settings, "nora_maintenance_window_minutes", 0))
        start_minutes_ago = int(getattr(settings, "nora_maintenance_window_start_minutes_ago", 0))

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
    dotted = _resolve_spectrum_oids(catalog)

    client = driver._client_factory(device)  # noqa: SLF001 — internal API
    try:
        noise_values: list[int] = []
        for name in SPECTRUM_NOISE_FLOOR_OID_NAMES:
            try:
                raw = client.get_oid(dotted[name])
            except KeyError:
                raw = 0
            noise_values.append(_coerce_int(raw))
    finally:
        try:
            client.close()
        except Exception:  # pragma: no cover - close is best-effort
            pass

    # Pair every frequency with its noise floor; sort ascending by
    # noise; cap to the top-N.
    paired: list[tuple[int, int]] = list(zip(_MHZ_BASE_FREQS, noise_values, strict=True))
    paired.sort(key=lambda pair: pair[1])
    ranked = [freq_mhz * _MHZ_TO_KHZ for freq_mhz, _ in paired[:DEFAULT_TOP_N]]
    noise_map: dict[str, float] = {
        f"{float(freq_mhz * _MHZ_TO_KHZ)}": float(noise)
        for freq_mhz, noise in zip(_MHZ_BASE_FREQS, noise_values, strict=True)
    }

    return SpectrumAnalysis(
        device_id=str(getattr(device, "host", device_id)),
        ranked_clean_frequencies=[float(x) for x in ranked],
        noise_floor_dbm=noise_map,
        scan_started_at=now.isoformat(),
    )


__all__ = [
    "SpectrumAnalysis",
    "fetch_spectrum",
    "SPECTRUM_NOISE_FLOOR_OID_NAMES",
    "DEFAULT_TOP_N",
]
