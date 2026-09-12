"""Pure correlation helpers — tower substring match + frequency-delta classification.

Two functions only; no I/O, no Pydantic models, no logging. These are
the algorithms behind `correlate_sector_interference` in `tools.py`.

Caveat (documented here AND in the tool docstring): substring match on
`tower_name` will false-match short prefixes. For example,
`tower_name="A"` matches every system containing the letter "A" (e.g.,
`AP-CORE-01`). A future enhancement is a structured `tower` field on
each record — out of scope for this slice.

Frequency classification (per spec R6):

- `delta < 0.5` MHz → `CO_CHANNEL` (drift tolerance; pure equality is
  rare on real radios).
- `0.5 <= delta < width` → `ADJACENT_CHANNEL`.
- `delta >= width` → `CLEAR`.
"""

from __future__ import annotations

from typing import Final, Literal

# Co-channel threshold (MHz). Anything within ±0.5 MHz of the target
# frequency is treated as co-channel interference, per radio domain
# convention.
_CO_CHANNEL_THRESHOLD_MHZ: Final[float] = 0.5

ConflictKind = Literal["CO_CHANNEL", "ADJACENT_CHANNEL", "CLEAR"]


def match_tower(system_name: str, tower_name: str) -> bool:
    """Return True when `tower_name` is a case-insensitive substring of `system_name`.

    Empty `tower_name` matches any `system_name` (substring of "" is
    always present). Empty `system_name` only matches empty `tower_name`.
    """
    return tower_name.lower() in system_name.lower()


def classify_conflict(carrier: float, target: float, width: float) -> ConflictKind:
    """Classify a frequency conflict.

    Args:
        carrier: The neighbor's carrier frequency in MHz.
        target: The proposed frequency in MHz.
        width: The channel width in MHz (e.g., 20.0 for 5 GHz Wi-Fi).

    Returns:
        `"CO_CHANNEL"` when `abs(carrier - target) < 0.5` MHz,
        `"ADJACENT_CHANNEL"` when `0.5 <= abs(...) < width`,
        `"CLEAR"` otherwise.
    """
    delta = abs(carrier - target)
    if delta < _CO_CHANNEL_THRESHOLD_MHZ:
        return "CO_CHANNEL"
    if delta < width:
        return "ADJACENT_CHANNEL"
    return "CLEAR"


__all__ = ["match_tower", "classify_conflict", "ConflictKind"]