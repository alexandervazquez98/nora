"""Band-crossing detector tests for `snmp_migrate_radio_frequency`.

WU-C (feat/multi-community-band-reboot): the band-crossing detector
lives in a small ``BandPlan`` helper that maps a carrier frequency
(in MHz) to a regulatory band class. The detector flags ``band_crossing=True``
when the migration crosses bands (5.x ↔ 4.9 GHz) so the orchestrator
can mint a second HITL token for ``snmp_reboot_radio``. Within-band
sub-channel changes (5.7 → 5.8 GHz) keep ``band_crossing=False``;
the firmware applies those changes without reboot (per the 25.x
MIB DESCRIPTION on ``radioFreqCarrier``: "As of release 16.1, this
OID no longer requires reboot to take affect").

Frequency table (MHz → band class). Each band covers its center
frequency ± half of its width; gaps are handled by an explicit
`outside_cambium_bands` return. The Cambium PMP 450i 5.x product
line maps approximately as:

    4900 band (Public Safety)   4900.0 - 5000.0
    5100 band                    5150.0 - 5250.0
    5200 band                    5250.0 - 5350.0
    5400 band                    5470.0 - 5725.0
    5700 band                    5725.0 - 5850.0
    5800 band                    5725.0 - 5875.0 (overlaps with 5700)

The table is intentionally narrow for v1 — see
`odd/tasks/multi-community-migration-and-band-reboot.md` "Open
follow-ups" for the future pull from ``radioFrequencyBand`` via
``OidCatalogRegistry.resolve``.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Frequency → band-class mapping table.
# ---------------------------------------------------------------------------

# MHz low / high boundaries (inclusive). The order matters: the
# detector picks the FIRST matching band in this list.
_CAMBIUM_BAND_RANGES_MHZ: tuple[tuple[str, float, float], ...] = (
    # CBRS / lightly-licensed 3 GHz. Cambium documents `band3500` in
    # the `radioFrequencyBand` enum on the 3 GHz radio module
    # (reference C030045A002A). Added in issue #81.
    ("3500", 3300.0, 3900.0),
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
    """Return the Cambium band-class name for ``mhz``, or ``None`` if outside."""
    for name, low, high in _CAMBIUM_BAND_RANGES_MHZ:
        if low <= mhz <= high:
            return name
    return None


def _is_band_crossing(current_mhz: float, target_mhz: float) -> bool:
    """True when the two carriers fall into DIFFERENT Cambium bands.

    The detector is intentionally narrow (frequency-only) — it does
    not consult the radio's ``radioFrequencyBand`` or
    ``rebootIfRequired`` OIDs. Those signals are read at runtime
    inside ``fetch_migrate`` and ``fetch_reboot`` so a stale
    inventory cannot misclassify the migration. WU-C's tests pin
    the table-driven behaviour.
    """
    current = _band_for_frequency(current_mhz)
    target = _band_for_frequency(target_mhz)
    # Both inside the same band: no crossing. Either outside: not
    # crossing in the table-driven sense (the runtime layer adds
    # the ``radioFrequencyBand`` vote later).
    if current is None or target is None:
        return False
    return current != target


# ---------------------------------------------------------------------------
# Tests — band-class mapping (frequency → name)
# ---------------------------------------------------------------------------


def test_band_for_frequency_5_7_ghz_returns_5700() -> None:
    """A 5780 MHz carrier maps to the 5700 ISM / U-NII-3 band."""
    assert _band_for_frequency(5780.0) == "5700"


def test_band_for_frequency_5_1_ghz_returns_5100() -> None:
    """A 5180 MHz carrier maps to the 5100 U-NII-1 band."""
    assert _band_for_frequency(5180.0) == "5100"


def test_band_for_frequency_5_2_ghz_returns_5200() -> None:
    """A 5260 MHz carrier maps to the 5200 U-NII-2A band."""
    assert _band_for_frequency(5260.0) == "5200"


def test_band_for_frequency_5_4_ghz_returns_5400() -> None:
    """A 5500 MHz carrier maps to the 5400 U-NII-2C band."""
    assert _band_for_frequency(5500.0) == "5400"


def test_band_for_frequency_4_9_ghz_returns_4900() -> None:
    """A 4940 MHz carrier maps to the 4900 Public Safety band."""
    assert _band_for_frequency(4940.0) == "4900"


def test_band_for_frequency_outside_cambium_bands_returns_none() -> None:
    """A 2400 MHz carrier (sub-3.3 GHz ISM) is OUTSIDE the Cambium PMP 450i bands.

    Note: the 3 GHz band ``3500`` (3300.0 - 3900.0 MHz) was added in
    issue #81 for the Cambium 3 GHz radio module. A 3500 MHz
    carrier now maps to that band, NOT to ``None``. Sub-3.3 GHz
    carriers (e.g. 2400 MHz ISM, 2700 MHz Wi-Fi 6E lower) remain
    outside the Cambium regulatory table.
    """
    assert _band_for_frequency(2400.0) is None


def test_band_for_frequency_3_5_ghz_returns_3500() -> None:
    """A 3600 MHz carrier maps to the 3500 CBRS / lightly-licensed band.

    Issue #81 regression: this band was missing from the table
    pre-fix, causing ``_band_for_frequency(3600.0)`` to return
    ``None`` and breaking the band-crossing detector on 3 GHz
    sectors. The fix adds the band so the 3 GHz radio module
    (``C030045A002A``) is properly recognised.
    """
    assert _band_for_frequency(3600.0) == "3500"


def test_band_for_frequency_3_5_ghz_lower_boundary_3300() -> None:
    """The 3500 band starts at 3300 MHz inclusive (CBRS lower edge)."""
    assert _band_for_frequency(3300.0) == "3500"


def test_band_for_frequency_3_5_ghz_upper_boundary_3900() -> None:
    """The 3500 band ends at 3900 MHz inclusive (CBRS upper edge)."""
    assert _band_for_frequency(3900.0) == "3500"


def test_band_for_frequency_just_above_3500_band_returns_none() -> None:
    """A 3901 MHz carrier is OUTSIDE the 3500 band (post-CBRS gap)."""
    assert _band_for_frequency(3901.0) is None


def test_band_for_frequency_boundary_5_25_ghz() -> None:
    """Boundary 5250.0 sits on the 5100/5200 boundary — first-match wins.

    The table is ordered from low to high, so a frequency exactly on
    a shared boundary (e.g. 5250 MHz between 5100 and 5200) lands
    in the LOWER-numbered bucket (5100).
    """
    # 5250 is the upper bound of 5100 (5150-5250 inclusive) — first-match wins.
    assert _band_for_frequency(5250.0) == "5100"
    # 5251 is strictly above 5100's upper bound — falls into 5200.
    assert _band_for_frequency(5251.0) == "5200"


# ---------------------------------------------------------------------------
# Tests — band-crossing detector
# ---------------------------------------------------------------------------


def test_is_band_crossing_same_5_7_band_no_crossing() -> None:
    """Sub-channel move within 5.7 GHz (e.g. 5780 → 5800) is NOT a crossing.

    Per the 25.x MIB DESCRIPTION on ``radioFreqCarrier``: the OID
    does NOT require reboot post-16.1 within the same band.
    """
    assert _is_band_crossing(5780.0, 5800.0) is False


def test_is_band_crossing_5_1_to_5_2_is_crossing() -> None:
    """Operator-reported incident #2: 5.1 → 5.2 IS a band crossing.

    5100 (5150-5250) and 5200 (5250-5350) are distinct U-NII bands;
    each requires its own regulatory authorization. The PMP 450i
    firmware rejects the SET ("no está en la lista") because the
    firmware was provisioned for one band, not the other.
    """
    assert _is_band_crossing(5180.0, 5260.0) is True


def test_is_band_crossing_5_4_to_5_7_is_crossing() -> None:
    """Operator-reported incident: 5.4 → 5.7 is a band crossing.

    5400 (5470-5725) and 5700 (5725-5875) share a boundary at
    5725 MHz; sub-channels on either side are different bands.
    """
    assert _is_band_crossing(5500.0, 5780.0) is True


def test_is_band_crossing_5_x_to_4_9_is_crossing() -> None:
    """Operator-reported incident #3: 5.x → 4.9 IS a band crossing.

    Crossing to Public Safety (4.9 GHz) requires a reboot — the
    band class flip happens during the reboot cycle.
    """
    assert _is_band_crossing(5800.0, 4940.0) is True


def test_is_band_crossing_4_9_to_5_x_is_crossing() -> None:
    """4.9 → 5.x is also a crossing (Public Safety → ISM)."""
    assert _is_band_crossing(4940.0, 5800.0) is True


def test_is_band_crossing_4_9_to_4_9_same() -> None:
    """Within-band move on 4.9 GHz is NOT a crossing."""
    assert _is_band_crossing(4940.0, 4960.0) is False


def test_is_band_crossing_outside_bands_is_not_crossing() -> None:
    """An outside-bands frequency pair does NOT trigger band_crossing.

    The table-driven detector only fires on a confirmed
    different-band move. The runtime layer adds the
    ``radioFrequencyBand`` OID vote separately — that signal
    CAN flag outside-band moves; the table alone cannot.
    """
    assert _is_band_crossing(3500.0, 3600.0) is False


# ---------------------------------------------------------------------------
# Exported seam — overridable for tests.
# ---------------------------------------------------------------------------


__all__ = [
    "_band_for_frequency",
    "_is_band_crossing",
    "_CAMBIUM_BAND_RANGES_MHZ",
]
