"""Tests for the typed driver exception hierarchy.

Each spec scenario maps to a test that verifies a specific contract from
`specs/driver-snmp-pmp450i/spec.md` and `specs/oid-catalog/spec.md`:

* Driver-R6-S3 — malformed OID surfaces a typed error with a sanitized message.
* Driver-R6-S1 — Device-not-found, network unreachable, and SNMP timeout
  each surface typed exceptions.
* OidCatalog-R2-S1 — unknown firmware pin raises `CatalogNotFoundError`.
* OidCatalog-R3-S2 — tampered catalog raises `CatalogVerificationError`.

Slice 1 adds three typed exceptions reserved for slices 4 + 5:
`AutonomousMutationRejected`, `MaintenanceWindowViolation`,
`UncataloguedToolError`. The classes themselves are stub-only — the
concrete raise sites land in their respective slices — but their
inheritance + constructor contract must be pinned here so PR 1 ends
with a wired seam.

The test file is RED until `src/nora/drivers/exceptions.py` exists with
the documented hierarchy.
"""

from __future__ import annotations

import pytest

from nora.drivers.exceptions import (
    AutonomousMutationRejected,
    CatalogNotFoundError,
    CatalogVerificationError,
    DeviceNotFoundError,
    DriverError,
    MaintenanceWindowViolation,
    NetworkUnreachableError,
    PromptNotFoundError,
    RefusesWriteError,
    SnmpTimeoutError,
    UncataloguedToolError,
)

# ---------------------------------------------------------------------------
# Hierarchy — every typed exception inherits from DriverError
# ---------------------------------------------------------------------------


def test_driver_error_is_exception_base_class() -> None:
    """`DriverError` is the single root of the hierarchy."""
    assert issubclass(DriverError, Exception)
    assert DriverError.__mro__[1] is Exception


@pytest.mark.parametrize(
    "exc_cls",
    [
        RefusesWriteError,
        DeviceNotFoundError,
        NetworkUnreachableError,
        SnmpTimeoutError,
        CatalogNotFoundError,
        CatalogVerificationError,
        PromptNotFoundError,
        # Slice-1 stubs that land in slices 4/5. The classes themselves
        # ship here; their raise sites land later.
        AutonomousMutationRejected,
        MaintenanceWindowViolation,
        UncataloguedToolError,
    ],
)
def test_every_driver_exception_inherits_from_driver_error(exc_cls: type[Exception]) -> None:
    """Every typed driver exception is a `DriverError` subclass.

    Catches the silent-fallback trap: a future refactor that introduces a
    bare `Exception` subclass cannot slip past this gate.
    """
    assert issubclass(exc_cls, DriverError), (
        f"{exc_cls.__name__} must inherit from DriverError, not bare Exception"
    )


def test_driver_error_carries_sanitizable_message() -> None:
    """Raising a `DriverError` with a message preserves the message verbatim.

    Driver-R3-S1: free-text error strings are sanitized at the tool
    boundary; the exception itself just carries the raw message.
    """
    exc = DriverError("connection refused at 10.0.0.5")
    assert "10.0.0.5" in str(exc)
    assert exc.args == ("connection refused at 10.0.0.5",)


# ---------------------------------------------------------------------------
# RefusesWriteError — Driver-R2
# ---------------------------------------------------------------------------


def test_refuses_write_error_message_format() -> None:
    """RefusesWriteError carries the offending identifier for diagnostics."""
    exc = RefusesWriteError("set")
    assert "set" in str(exc)
    assert isinstance(exc, DriverError)


# ---------------------------------------------------------------------------
# DeviceNotFoundError — Inventory + Driver-R6
# ---------------------------------------------------------------------------


def test_device_not_found_error_carries_device_id() -> None:
    """DeviceNotFoundError exposes the missing `device_id` for tool output."""
    exc = DeviceNotFoundError("ap-7400-01")
    assert "ap-7400-01" in str(exc)


def test_network_unreachable_error_carries_target() -> None:
    """NetworkUnreachableError surfaces the target host/port for diagnostics."""
    exc = NetworkUnreachableError("192.0.2.10:161")
    assert "192.0.2.10" in str(exc)


def test_snmp_timeout_error_carries_oid() -> None:
    """SnmpTimeoutError surfaces the OID that timed out."""
    exc = SnmpTimeoutError("1.3.6.1.4.1.161.19.3.1.1.1.0")
    assert "1.3.6.1.4.1.161.19.3.1.1.1.0" in str(exc)


# ---------------------------------------------------------------------------
# CatalogNotFoundError / CatalogVerificationError — OidCatalog-R2 / R3
# ---------------------------------------------------------------------------


def test_catalog_not_found_error_carries_ref() -> None:
    """CatalogNotFoundError carries the (vendor, model, firmware) ref tuple."""
    exc = CatalogNotFoundError(("cambium", "pmp450i", "99.0.0"))
    assert "cambium" in str(exc)
    assert "pmp450i" in str(exc)
    assert "99.0.0" in str(exc)


def test_catalog_verification_error_carries_path() -> None:
    """CatalogVerificationError exposes the file that failed verification."""
    exc = CatalogVerificationError(path="/data/oid-catalogs/x/y.json", reason="hmac mismatch")
    assert "/data/oid-catalogs/x/y.json" in str(exc)
    assert "hmac mismatch" in str(exc)


# ---------------------------------------------------------------------------
# PromptNotFoundError — Prompt-R4 / R5
# ---------------------------------------------------------------------------


def test_prompt_not_found_error_carries_name() -> None:
    """`PromptNotFoundError` exposes the missing prompt name."""
    exc = PromptNotFoundError("snmp_pmp450i")
    assert "snmp_pmp450i" in str(exc)


# ---------------------------------------------------------------------------
# Slice-1 stubs — AutonomousMutationRejected / MaintenanceWindowViolation /
# UncataloguedToolError. Concrete raise sites land in slices 4 + 5.
# ---------------------------------------------------------------------------


def test_autonomous_mutation_rejected_carries_hitl_token_message() -> None:
    """`AutonomousMutationRejected` carries the HITL-token-required message.

    The literal message is asserted in the slice-4 test
    `migrate_autonomous_call_raises_autonomous_mutation_rejected`; this
    constructor test pins the wording so the seam stays auditable
    before slice 4 wires its raise site.
    """
    exc = AutonomousMutationRejected(
        "autonomous device mutation rejected: HITL approval token required"
    )
    assert "HITL approval token required" in str(exc)
    assert isinstance(exc, DriverError)


def test_maintenance_window_violation_carries_window_state() -> None:
    """`MaintenanceWindowViolation` carries the violation context."""
    exc = MaintenanceWindowViolation("outside maintenance window (02:00-04:00 UTC)")
    assert "maintenance window" in str(exc)
    assert isinstance(exc, DriverError)


def test_uncatalogued_tool_error_carries_tool_name_and_reason() -> None:
    """`UncataloguedToolError` carries the tool name + reason string."""
    exc = UncataloguedToolError(
        tool_name="snmp_get_rogue_metric",
        reason="no OID catalog entry",
    )
    assert "snmp_get_rogue_metric" in str(exc)
    assert "no OID catalog entry" in str(exc)
    assert isinstance(exc, DriverError)


# ---------------------------------------------------------------------------
# Caught as DriverError — boundary polimorphism
# ---------------------------------------------------------------------------


def test_callers_can_catch_whole_hierarchy_with_driver_error() -> None:
    """A single `except DriverError` catches every typed driver failure."""
    # CatalogVerificationError is the one exception whose constructor
    # takes keyword-only arguments (path + reason) because the on-disk
    # path is meaningful operationally — a bare string would lose it.
    catalog_exc: Exception = CatalogVerificationError(
        path="/tmp/catalog.json", reason="hmac mismatch"
    )
    try:
        raise catalog_exc
    except DriverError:
        pass  # expected
    else:  # pragma: no cover - safety net
        pytest.fail("CatalogVerificationError was not caught by `except DriverError`")

    for exc_cls in (
        RefusesWriteError,
        DeviceNotFoundError,
        NetworkUnreachableError,
        SnmpTimeoutError,
        CatalogNotFoundError,
        PromptNotFoundError,
        AutonomousMutationRejected,
        MaintenanceWindowViolation,
    ):
        try:
            raise exc_cls("probe")
        except DriverError:
            pass  # expected
        else:  # pragma: no cover - safety net
            pytest.fail(f"{exc_cls.__name__} was not caught by `except DriverError`")

    # `UncataloguedToolError` takes keyword-only args (tool_name + reason)
    # because the slice-5 boot guard needs the offending tool name in the
    # exception payload. Catch it with the dedicated kwargs instead.
    try:
        raise UncataloguedToolError(tool_name="probe", reason="probe")
    except DriverError:
        pass  # expected
    else:  # pragma: no cover - safety net
        pytest.fail("UncataloguedToolError was not caught by `except DriverError`")


# ---------------------------------------------------------------------------
# 2026-09-15-register-device-mcp — typed errors for `register_device`.
#
# Each new exception subclasses `DriverError`, carries its offeding
# identifier verbatim (host / community / device_id), and is caught by
# the existing boundary polymorphism test above once added there.
# ---------------------------------------------------------------------------


def test_device_unreachable_is_driver_error_subclass() -> None:
    """`DeviceUnreachable` is a `DriverError` carrying the host string."""
    from nora.drivers.exceptions import DeviceUnreachable

    exc = DeviceUnreachable("192.0.2.10")
    assert isinstance(exc, DriverError)
    assert "192.0.2.10" in str(exc)


def test_invalid_community_is_driver_error_subclass() -> None:
    """`InvalidCommunity` is a `DriverError` carrying the community string."""
    from nora.drivers.exceptions import InvalidCommunity

    exc = InvalidCommunity("bogus-community")
    assert isinstance(exc, DriverError)
    assert "bogus-community" in str(exc)


def test_invalid_host_error_is_driver_error_subclass() -> None:
    """`InvalidHostError` is a `DriverError` carrying the malformed host."""
    from nora.drivers.exceptions import InvalidHostError

    exc = InvalidHostError("not-an-ip")
    assert isinstance(exc, DriverError)
    assert "not-an-ip" in str(exc)


def test_duplicate_device_error_is_driver_error_subclass() -> None:
    """`DuplicateDeviceError` is a `DriverError` carrying the colliding id."""
    from nora.drivers.exceptions import DuplicateDeviceError

    exc = DuplicateDeviceError("ap-7400-01")
    assert isinstance(exc, DriverError)
    assert "ap-7400-01" in str(exc)
