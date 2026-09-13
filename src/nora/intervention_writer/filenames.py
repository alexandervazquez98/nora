"""Filename builder for the intervention writer (W1 + W2).

Template: ``INT-<ticket>-<ip>-<unix>-<6hex>.json``.

`<ticket>` MUST match ``^[A-Za-z0-9_-]+$`` — strict alphanumeric,
underscore, hyphen. Anything else (forward slash, dot, NUL byte,
whitespace, unicode) raises `InvalidFilenameComponent`.

`<ip>` allows dots and colons so IPv4 literals like ``10.0.0.1`` and
IPv6 literals like ``fe80::1`` work, but rejects path-traversal
characters: forward slash, NUL byte, whitespace, ``..`` substring,
unicode. The pattern is ``^[A-Za-z0-9_.:-]+$`` plus a `..` substring
check.

The unix stamp is interpolated verbatim and is the caller's
responsibility (the writer passes `int(time.time())`); the 6-hex
suffix is also caller-supplied so the collision-retry layer can roll a
fresh one without re-validating the components.

The function is pure (no I/O, no clock, no randomness) — tests pass a
fixed `unix` + `hex_suffix` for deterministic assertions.
"""

from __future__ import annotations

import re
from typing import Any, Final

# Allowed character set for `<ticket>` (strict; no dots).
_TICKET_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]+$")

# Allowed character set for `<ip>` (allows dots for IPv4 and colons
# for IPv6). Whitespace, slashes, NUL, unicode are still rejected.
_IP_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_.:-]+$")


class InvalidFilenameComponent(ValueError):
    """Raised when a filename component contains characters outside the safe set.

    Carries the offending `field` name and the raw `value` so callers
    (the MCP wrapper in particular) can surface a precise error without
    echoing the literal back to the wire.
    """

    def __init__(self, field: str, value: str) -> None:
        super().__init__(f"invalid filename component: {field!r}={value!r}")
        self.field: str = field
        self.value: str = value


def _validate_component(field: str, value: Any, pattern: re.Pattern[str]) -> str:
    """Return `value` if it matches `pattern`, else raise `InvalidFilenameComponent`.

    The function coerces non-str values via `str(...)` for robustness
    against JSON-RPC payload drift, but rejects anything that does not
    match the safe-character regex.
    """
    if not isinstance(value, str):
        # Pydantic should have rejected non-strings before we got here;
        # this is defensive so a future payload drift doesn't bypass the
        # regex silently.
        raise InvalidFilenameComponent(field, str(value))
    if not pattern.match(value):
        raise InvalidFilenameComponent(field, value)
    # Defense-in-depth: reject `..` substring in the IP component to
    # close the `/foo/../bar` traversal trick the regex alone misses.
    if field == "target_ip" and ".." in value:
        raise InvalidFilenameComponent(field, value)
    return value


def build_filename(payload: dict[str, Any], unix: int, hex_suffix: str) -> str:
    """Build ``INT-<ticket>-<ip>-<unix>-<6hex>.json`` from `payload`.

    Args:
        payload: A dict with `ticket_number` and `target_ip` keys.
        unix: The 10-digit epoch seconds the writer will pin the record to.
        hex_suffix: 6 random hex chars for the uniqueness suffix.

    Returns:
        The full filename (no directory component).

    Raises:
        InvalidFilenameComponent: when `ticket_number` or `target_ip`
            contain characters outside their respective safe sets. The
            error names the field and echoes the offending value so the
            MCP wrapper can return a precise `{"status": "INVALID_INPUT", ...}`
            dict without leaking the value to the wire.
    """
    ticket: str = _validate_component("ticket_number", payload["ticket_number"], _TICKET_RE)
    ip: str = _validate_component("target_ip", payload["target_ip"], _IP_RE)
    return f"INT-{ticket}-{ip}-{unix}-{hex_suffix}.json"


__all__ = ["InvalidFilenameComponent", "build_filename"]
