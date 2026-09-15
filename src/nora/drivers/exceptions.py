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

Slice-1 stubs (raise sites land in PR 4 + PR 5):

* `AutonomousMutationRejected`  — slice 4 (HITL-gated migration).
* `MaintenanceWindowViolation`  — slice 4 (spectrum sweep window).
* `UncataloguedToolError`       — slice 5 (registration-time guard).

The classes are intentionally defined here ahead of their raise sites
so that downstream code can `import` the names and wire them into
`except` clauses without requiring slice 4/5 to land first.
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


# ---------------------------------------------------------------------------
# Slice-1 stubs — concrete raise sites land in PR 4 (HITL-gated migration +
# spectrum sweep) and PR 5 (tool-registration guard). The classes are
# defined here so downstream imports stay stable across the chain.
# ---------------------------------------------------------------------------


class AutonomousMutationRejected(DriverError):
    """Raised when code attempts to mutate a device without an HITL token.

    Slice 4 / PR 4 — the migration tool (`snmp_migrate_radio_frequency`)
    calls `hitl.tokens.verify_approval_token(...)` first; missing or
    invalid tokens raise this exception. The literal wording
    ``"autonomous device mutation rejected: HITL approval token
    required"`` is asserted in
    `tests/test_hitl_tokens.py::migrate_autonomous_call_raises_autonomous_mutation_rejected`
    (slice 4); pinning it here as the canonical exception keeps the
    contract auditable before slice 4 lands.

    Slice-1 invariant: the class MUST inherit from `DriverError` so a
    caller writing `except DriverError` catches it without special-case
    imports.
    """


class MaintenanceWindowViolation(DriverError):
    """Raised when a tool call lands outside the operator's maintenance window.

    Slice 4 / PR 4 — `snmp_pmp450i.spectrum.fetch_spectrum` queries
    `Settings.nora_maintenance_window_*` and refuses the call when
    ``now`` falls outside the configured window. The typed exception
    lets the caller distinguish "we're not allowed to touch this now"
    from generic `NetworkUnreachableError` / `SnmpTimeoutError`.

    Defined here as a stub so the spectrum module can import the
    name at PR 4 without waiting on this module to grow.
    """


class UncataloguedToolError(DriverError):
    """Raised when an `@mcp.tool` is registered without an OID catalog entry.

    Slice 5 / PR 5 — `cli.py` walks the registered tools at boot and
    raises this for any tool name absent from
    `OidCatalogRegistry.required_oids_by_tool((vendor, model))`.
    Without this guard, slice 2/3/4 tools could ship with no catalog
    reference and silently miss the runtime warning from
    `OidCatalogRegistry.resolve`.

    Constructor takes ``tool_name`` and ``reason`` (both keyword-only)
    so the boot error message points at the offending tool.
    """

    def __init__(self, *, tool_name: str, reason: str) -> None:
        super().__init__(f"{tool_name}: {reason}")
        self.tool_name = tool_name
        self.reason = reason


class DuplicateDeviceError(DriverError):
    """Raised when `MutableInventory.register` collides on an existing `device_id`.

    `MutableInventory` wrapper (issue #42 / change
    `2026-09-15-register-device-mcp`): preserves `Inventory.frozen=True`
    while allowing ad-hoc `register_device` inserts. The wrapper rejects
    duplicate ids at insertion time so the operator gets a typed error
    instead of a silent overwrite. Carries the offending id verbatim.
    """


class DeviceUnreachable(DriverError):
    """Raised when the SNMP agent's port is closed or no response is received.

    Issue #42 / change `2026-09-15-register-device-mcp`: the
    `register_device` validate path distinguishes wire-level failures
    (`OSError` / `TimeoutError`) from auth-rejected ones
    (`InvalidCommunity`) so the orchestrator can show a typed error per
    failure mode. Carries the offending host string verbatim via the
    `.host` attribute (the message is the same string, by convention).
    """

    def __init__(self, host: str) -> None:
        super().__init__(host)
        self.host = host


class InvalidCommunity(DriverError):
    """Raised when the agent rejects the supplied community string.

    Issue #42 / change `2026-09-15-register-device-mcp`: the
    `register_device` validate path maps `puresnmp.exc.SnmpError` to
    this typed error so the orchestrator sees a distinct error code
    (auth-rejected, not unreachable). Carries the offending community
    string verbatim via the `.community` attribute (the operator typed
    it; the tool boundary sanitises before serialisation).
    """

    def __init__(self, community: str) -> None:
        super().__init__(community)
        self.community = community


class InvalidHostError(DriverError):
    """Raised when the supplied host string fails the IPv4-literal contract.

    Issue #42 / change `2026-09-15-register-device-mcp`: the
    `register_device` Pydantic boundary validates the host BEFORE any
    wire frame so a malformed value (e.g. `not-an-ip`) raises a typed
    error without sending any traffic. Carries the offending host
    string verbatim via the `.host` attribute.
    """

    def __init__(self, host: str) -> None:
        super().__init__(host)
        self.host = host


__all__ = [
    "DriverError",
    "RefusesWriteError",
    "DeviceNotFoundError",
    "NetworkUnreachableError",
    "SnmpTimeoutError",
    "CatalogNotFoundError",
    "CatalogVerificationError",
    "PromptNotFoundError",
    # Slice-1 stubs (raise sites in PR 4 + PR 5).
    "AutonomousMutationRejected",
    "MaintenanceWindowViolation",
    "UncataloguedToolError",
    # Issue #42 / `2026-09-15-register-device-mcp` — typed errors for
    # the `register_device` tool surface.
    "DuplicateDeviceError",
    "DeviceUnreachable",
    "InvalidCommunity",
    "InvalidHostError",
]
