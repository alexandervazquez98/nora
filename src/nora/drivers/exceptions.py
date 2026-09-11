"""Typed exception hierarchy for the driver layer.

One shared module so every component (inventory, OID catalog, prompt
registry, SNMP clients, MCP facade) raises from the same vocabulary. A
caller that writes `except DriverError` catches the entire surface; a
caller that needs to distinguish one failure mode catches a single
subclass.

Contract map (from the change specs):

* `RefusesWriteError`           — Driver-R2: any write op attempt.
* `DeviceNotFoundError`         — Driver-R6: inventory miss.
* `NetworkUnreachableError`     — Driver-R6: agent unreachable.
* `SnmpTimeoutError`            — Driver-R6: wire timeout / no response.
* `CatalogNotFoundError`        — OidCatalog-R2: unknown firmware pin.
* `CatalogVerificationError`    — OidCatalog-R3 / R4 / R5: HMAC failure,
                                   schema failure, or key rotation.
* `PromptNotFoundError`         — Prompt-R4 / R5: missing or invalid
                                   prompt at boot.
"""

from __future__ import annotations

from pathlib import Path


class DriverError(Exception):
    """Base class for every typed driver-layer error.

    Single root of the hierarchy so callers can write a single
    ``except DriverError`` to catch the entire surface. The class also
    carries a verbatim message — Driver-R3 requires free-text errors to
    be sanitized at the tool boundary, NOT at the exception site.
    """


class RefusesWriteError(DriverError):
    """Raised when code tries to invoke a write verb (set / update / etc.).

    Driver-R2. The exception carries the offending identifier so the
    autotrace middleware records a useful diagnostic in the journal.
    """


class DeviceNotFoundError(DriverError):
    """Raised when the inventory has no entry for the requested `device_id`.

    Driver-R6-S1, Inventory contract. Carries the offending id verbatim.
    """


class NetworkUnreachableError(DriverError):
    """Raised when the SNMP agent's port is closed / host unreachable.

    Driver-R6-S1. Carries the ``host:port`` target string for diagnostic
    context; the tool surface sanitises it before persistence.
    """


class SnmpTimeoutError(DriverError):
    """Raised when an SNMP GET exceeds the configured timeout.

    Driver-R6-S1. Carries the dotted-OID that timed out.
    """


class CatalogNotFoundError(DriverError):
    """Raised when no catalog file exists for the requested firmware pin.

    OidCatalog-R2. Carries the ``(vendor, model, firmware)`` ref tuple.
    """


class CatalogVerificationError(DriverError):
    """Raised when a catalog file fails HMAC verification or schema check.

    OidCatalog-R3 (HMAC), R4 (key rotation), R5 (schema). Carries the
    offending path and a short reason string so the on-screen diagnostic
    points at the file the operator needs to re-sign.
    """

    def __init__(self, *, path: str | Path, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = Path(path)
        self.reason = reason


class PromptNotFoundError(DriverError):
    """Raised when a prompt cannot be located or fails front-matter validation.

    Prompt-R4 / R5. Carries the offending prompt name so the boot error
    message points the operator at the missing file.
    """


__all__ = [
    "DriverError",
    "RefusesWriteError",
    "DeviceNotFoundError",
    "NetworkUnreachableError",
    "SnmpTimeoutError",
    "CatalogNotFoundError",
    "CatalogVerificationError",
    "PromptNotFoundError",
]
