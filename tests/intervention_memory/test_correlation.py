"""Correlation pure-function tests — covers R6 (tower + frequency match).

Two functions under test:

- `match_tower(system_name, tower_name)`: substring lower() match.
- `classify_conflict(carrier, target, width)`: CO_CHANNEL / ADJACENT_CHANNEL / CLEAR.

Documented caveat (per design §6): substring match false-positives on
short prefixes (e.g., `tower_name="A"` matches every `*-A` AP). A
structured `tower` field is the future fix (out of scope).
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# `match_tower`
# ---------------------------------------------------------------------------


def test_match_tower_substring_case_insensitive() -> None:
    """`TWR-ISABEL` matches `TWR-ISABEL-5GHZ-A` regardless of case."""
    from nora.intervention_memory.correlation import match_tower

    assert match_tower("TWR-ISABEL-5GHZ-A", "TWR-ISABEL") is True
    assert match_tower("twr-isabel-5ghz-a", "TWR-ISABEL") is True
    assert match_tower("TWR-ISABEL-5GHZ-A", "twr-isabel") is True


def test_match_tower_returns_false_when_substring_absent() -> None:
    """`TWR-PASO` does NOT match a `TWR-ISABEL` system."""
    from nora.intervention_memory.correlation import match_tower

    assert match_tower("TWR-ISABEL-5GHZ-A", "TWR-PASO") is False
    assert match_tower("AP-CORE-01", "TWR") is False


def test_match_tower_handles_empty_strings() -> None:
    """Empty `tower_name` matches anything; empty `system_name` matches only empty `tower_name`."""
    from nora.intervention_memory.correlation import match_tower

    # Empty tower_name → "" in anything → True (substring match).
    assert match_tower("anything", "") is True
    # Empty system_name, non-empty tower_name → never matches.
    assert match_tower("", "TWR-ISABEL") is False


def test_match_tower_documents_substring_false_positive() -> None:
    """Document the `tower_name="A"` false-match caveat.

    This test pins the known caveat so any future fix to a structured
    tower field updates this test to fail until the fix lands.
    """
    from nora.intervention_memory.correlation import match_tower

    # tower_name="A" matches every system containing the letter "A".
    assert match_tower("AP-CORE-01", "A") is True  # noqa: documented caveat


# ---------------------------------------------------------------------------
# `classify_conflict`
# ---------------------------------------------------------------------------


def test_classify_conflict_equal_carrier_returns_co_channel() -> None:
    """`carrier == target` → CO_CHANNEL."""
    from nora.intervention_memory.correlation import classify_conflict

    assert classify_conflict(5760.0, 5760.0, 20.0) == "CO_CHANNEL"
    assert classify_conflict(0.0, 0.0, 10.0) == "CO_CHANNEL"


def test_classify_conflict_nearby_carrier_returns_adjacent() -> None:
    """`0 < delta < width` → ADJACENT_CHANNEL."""
    from nora.intervention_memory.correlation import classify_conflict

    assert classify_conflict(5770.0, 5760.0, 20.0) == "ADJACENT_CHANNEL"
    assert classify_conflict(5750.0, 5760.0, 20.0) == "ADJACENT_CHANNEL"


def test_classify_conflict_clear_when_delt_a_exceeds_width() -> None:
    """`delta >= width` → CLEAR (strict `<`)."""
    from nora.intervention_memory.correlation import classify_conflict

    # delta == width → boundary; strict < → CLEAR.
    assert classify_conflict(5780.0, 5760.0, 20.0) == "CLEAR"
    # delta >> width → CLEAR.
    assert classify_conflict(5900.0, 5760.0, 20.0) == "CLEAR"


def test_classify_conflict_delta_below_half_is_co_channel() -> None:
    """`delta < 0.5` MHz → CO_CHANNEL even if `delta != 0` (drift tolerance)."""
    from nora.intervention_memory.correlation import classify_conflict

    # delta = 0.3 MHz, well below the 0.5 MHz co-channel threshold.
    assert classify_conflict(5760.3, 5760.0, 20.0) == "CO_CHANNEL"
    assert classify_conflict(5759.7, 5760.0, 20.0) == "CO_CHANNEL"


def test_classify_conflict_delta_at_half_is_adjacent() -> None:
    """`delta == 0.5` MHz → ADJACENT_CHANNEL (boundary is strict `< 0.5`)."""
    from nora.intervention_memory.correlation import classify_conflict

    assert classify_conflict(5760.5, 5760.0, 20.0) == "ADJACENT_CHANNEL"


def test_classify_conflict_uses_absolute_delta() -> None:
    """Negative deltas (carrier < target) are treated identically to positive."""
    from nora.intervention_memory.correlation import classify_conflict

    assert classify_conflict(5750.0, 5760.0, 20.0) == "ADJACENT_CHANNEL"
    assert classify_conflict(5770.0, 5760.0, 20.0) == "ADJACENT_CHANNEL"
    # Both directions at delta == 0 → CO_CHANNEL.
    assert classify_conflict(5760.0, 5760.0, 20.0) == "CO_CHANNEL"


# ---------------------------------------------------------------------------
# Triangulate: integration of both functions in a correlation scenario
# ---------------------------------------------------------------------------


def test_correlation_combined_match_and_classify() -> None:
    """End-to-end tower + frequency check used by `correlate_sector_interference`."""
    from nora.intervention_memory.correlation import classify_conflict, match_tower

    system_name = "TWR-ISABEL-5GHZ-A"
    carrier = 5770.0
    target = 5760.0
    width = 20.0

    assert match_tower(system_name, "TWR-ISABEL") is True
    assert classify_conflict(carrier, target, width) == "ADJACENT_CHANNEL"


def test_correlation_tower_mismatch_bypasses_classification() -> None:
    """When the tower doesn't match, classification is irrelevant — zero conflicts."""
    from nora.intervention_memory.correlation import match_tower

    assert match_tower("TWR-PASO-5GHZ-A", "TWR-ISABEL") is False
