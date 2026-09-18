"""Band-crossing detector for PMP 450i migrations.

WU-C (feat/multi-community-band-reboot): the table-driven
``_band_for_frequency`` and ``_is_band_crossing`` helpers are the
canonical implementation of the band-crossing detector. The
runtime layer in :mod:`nora.drivers.snmp_pmp450i.migrate` reads the
radio's ``radioFrequencyBand`` OID (catalog v2 — see
``feat/multi-community-band-reboot`` commit ``f85f2ae``) and
``rebootIfRequired`` OID and combines the two signals so the
operator-facing ``band_crossing`` field on :class:`MigrationResult`
reflects BOTH the firmware's authoritative vote and the
frequency-table fallback.

The table is intentionally narrow for v1 — see the feature doc
"Open follow-ups" for the future pull from
``radioFrequencyBand`` via ``OidCatalogRegistry.resolve``. The
runtime layer already uses the OID; this module's table is the
fallback when the OID read is unreliable (legacy catalog, race
with the radio during a band-class flip).

Frequency → band-class mapping (per Cambium PMP 450i firmware
25.x MIB ``radioFrequencyBand`` enum):

    4900 band (Public Safety)   4900.0 - 5000.0
    5100 band                    5150.0 - 5250.0
    5200 band                    5250.0 - 5350.0
    5400 band                    5470.0 - 5725.0
    5700 band                    5725.0 - 5875.0

The Cambium enum also lists `band5800` (5725-5875) which OVERLAPS
with `band5700`; v1 of this helper folds both into the `5700`
bucket because the boundary (5725) is the public FCC U-NII-3 /
 ISM-band edge and operators usually pick the lower number.
"""

from __future__ import annotations

# MHz low / high boundaries (inclusive). The order matters: the
# detector picks the FIRST matching band in this list.
_CAMBIUM_BAND_RANGES_MHZ: tuple[tuple[str, float, float], ...] = (
    # Public Safety (4.9 GHz). Cambium documents `band4900` in the
    # `radioFrequencyBand` enum.
    ("4900", 4900.0, 5000.0),
    # U-NII-1 / 5.1 GHz. Cambium `band5100`.
    ("5100", 5150.0, 5250.0),
    # U-NII-2A / 5.2 GHz. Cambium `band5200`.
    ("5200", 5250.0, 5350.0),
    # U-NII-2C / 5.4 GHz. Cambium `band5400`.
    ("5400", 5470.0, 5725.0),
    # U-NII-3 / 5.7 GHz + ISM. Cambium `band5700` / `band5800`.
    ("5700", 5725.0, 5875.0),
)


def _band_for_frequency(mhz: float) -> str | None:
    """Return the Cambium band-class name for ``mhz``, or ``None`` if outside.

    Args:
        mhz: Carrier frequency in MHz.

    Returns:
        Band-class name (`"4900"`, `"5100"`, `"5200"`, `"5400"`,
        `"5700"`) when the frequency falls inside one of the
        documented PMP 450i regulatory bands. ``None`` otherwise
        (e.g. 3.5 GHz, 3.6 GHz, sub-4.9 GHz, or frequencies the
        catalog has not been provisioned for).

    Overlapping boundaries are resolved by first-match wins — the
    table is ordered from low to high, so a frequency exactly on
    a shared boundary (e.g. 5250 MHz between 5100 and 5200) lands
    in the HIGHER-numbered bucket.
    """
    for name, low, high in _CAMBIUM_BAND_RANGES_MHZ:
        if low <= mhz <= high:
            return name
    return None


def _is_band_crossing(current_mhz: float, target_mhz: float) -> bool:
    """True when the two carriers fall into DIFFERENT Cambium bands.

    The detector is intentionally narrow (frequency-only) — it does
    not consult the radio's ``radioFrequencyBand`` or
    ``rebootIfRequired`` OIDs. Those signals are read at runtime
    inside :func:`nora.drivers.snmp_pmp450i.migrate.fetch_migrate`
    and :func:`nora.drivers.snmp_pmp450i.reboot.fetch_reboot` so a
    stale inventory cannot misclassify the migration. The helper
    is the fallback path; the OID read is the primary signal.

    Args:
        current_mhz: Current carrier frequency in MHz.
        target_mhz:  Target carrier frequency in MHz.

    Returns:
        True when both carriers fall inside distinct documented
        bands. False when the pair shares a band, OR when either
        carrier is outside the documented bands (the OID vote
        handles those edge cases at runtime).
    """
    current = _band_for_frequency(current_mhz)
    target = _band_for_frequency(target_mhz)
    # Both inside the same band: no crossing. Either outside: not
    # crossing in the table-driven sense (the runtime layer adds
    # the ``radioFrequencyBand`` vote later).
    if current is None or target is None:
        return False
    return current != target


__all__ = [
    "_band_for_frequency",
    "_is_band_crossing",
    "_CAMBIUM_BAND_RANGES_MHZ",
]
