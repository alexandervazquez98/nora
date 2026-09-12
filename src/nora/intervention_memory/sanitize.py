"""Recursive sanitizer walker for intervention memory records.

Walks every free-text field in a record through `Sanitizer.sanitize(...)`
and returns a JSON-safe `dict`. The bypass list of structured top-level
fields is inherited from `session-journal` R6 (`intervention_id`,
`target_ip`, `stage`, `status`, `timestamp_unix`, `timestamp_iso`,
`created_at`, `ticket_number`) — these are typed scalars, not free
text, and pass through unchanged.

User-locked decision (Q4 in proposal.md): `target_ip` returns verbatim
in tool output even though it is a private IPv4 literal. Other Sanitizer
rules still apply.

The walker mirrors `src/nora/core/session_journal.py:_sanitize_tree`:
dict → check bypass list first; list → recurse on items; str →
`sanitizer.sanitize(s).text`; other types → copy.
"""

from __future__ import annotations

from typing import Any, Final, FrozenSet

from nora.intervention_memory.models import InterventionMemoryRecord
from nora.sanitizer import Sanitizer

# Structured top-level fields that pass through the sanitizer unchanged.
# These are typed scalars / enum strings / UTC ISO timestamps — not free
# text authored by humans or LLMs.
_BYPASS_FIELDS: Final[FrozenSet[str]] = frozenset(
    {
        "intervention_id",
        "target_ip",
        "stage",
        "status",
        "timestamp_unix",
        "timestamp_iso",
        "created_at",
        "ticket_number",
    }
)


def _sanitize_value(value: Any, sanitizer: Sanitizer) -> Any:
    """Recursively walk `value`, sanitizing free-text strings in place.

    Pure: returns a new structure; never mutates the input.
    """
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            # Bypass list applies at the dict key level only — nested
            # bypass keys inside `network_equipment` (e.g., a future
            # `neighbor_device` field) are not in the bypass list.
            if isinstance(key, str) and key in _BYPASS_FIELDS:
                out[key] = item
            else:
                out[key] = _sanitize_value(item, sanitizer)
        return out
    if isinstance(value, list):
        return [_sanitize_value(v, sanitizer) for v in value]
    if isinstance(value, str):
        return sanitizer.sanitize(value).text
    return value


def sanitize_record_payload(
    record: InterventionMemoryRecord,
    sanitizer: Sanitizer,
) -> dict[str, Any]:
    """Walk a record through the sanitizer and return a JSON-safe dict.

    Args:
        record: The parsed `InterventionMemoryRecord`.
        sanitizer: The `Sanitizer` instance (per-session for stable aliases).

    Returns:
        A new dict, JSON-serialisable, with every free-text field
        sanitized and every bypass key preserved verbatim.
    """
    import json

    raw = record.model_dump(mode="json")
    sanitized = _sanitize_value(raw, sanitizer)
    # Round-trip through JSON to strip Pydantic-specific markers and
    # ensure the result is `dict[str, Any]` (no `dict[Any, Any]`).
    result: dict[str, Any] = json.loads(json.dumps(sanitized))
    return result


__all__ = [
    "sanitize_record_payload",
    "_sanitize_value",
    "_BYPASS_FIELDS",
]
