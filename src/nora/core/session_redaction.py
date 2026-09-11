"""R10 frozen redaction list.

The list of parameter NAMES whose VALUE MUST be replaced with the marker
`_REDACTION_MARKER` before any persistence (R10). The list is frozen by
the spec — do NOT extend or contract it without amending
`openspec/specs/session-journal/spec.md` and the R10 rationale.

The recursive `redact()` walker lives here too so the same module owns the
contract and the behaviour. Keeping it free of any pydantic coupling lets
us run redaction on untrusted input payloads before they're shaped into
`SessionStep`.
"""

from __future__ import annotations

from typing import Any, Final

# R10 — frozen. 11 keys; exact spelling matters (matches spec verbatim).
REDACTION_LIST: Final[frozenset[str]] = frozenset(
    {
        "community",
        "community_string",
        "auth_password",
        "auth_key",
        "priv_password",
        "priv_key",
        "password",
        "ssh_password",
        "api_key",
        "token",
        "secret",
    }
)

# Replacement marker. Stable string so a later read can detect that a value
# was redacted (rather than mistaking it for an empty value).
_REDACTION_MARKER: Final[str] = "[REDACTED]"


def redact(value: Any) -> Any:
    """Return a copy of `value` with every key matching `REDACTION_LIST` masked.

    Recursive: walks `dict` and `list` to arbitrary depth. Scalars are
    returned unchanged. The function is pure — it never mutates the input.
    """
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and key in REDACTION_LIST:
                out[key] = _REDACTION_MARKER
            else:
                out[key] = redact(item)
        return out
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


__all__ = ["REDACTION_LIST", "redact", "_REDACTION_MARKER"]
