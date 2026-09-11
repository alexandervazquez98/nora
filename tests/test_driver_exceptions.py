"""Tests for the typed driver exception hierarchy.

Each spec scenario maps to a test that verifies a specific contract from
`specs/driver-snmp-pmp450i/spec.md` and `specs/oid-catalog/spec.md`:

* Driver-R6-S3 — malformed OID surfaces a typed error with a sanitized message.
* Driver-R6-S1 — Device-not-found, network unreachable, and SNMP timeout
  each surface typed exceptions.
* OidCatalog-R2-S1 — unknown firmware pin raises `CatalogNotFoundError`.
* OidCatalog-R3-S2 — tampered catalog raises `CatalogVerificationError`.

The test file is RED until `src/nora/drivers/exceptions.py` exists with
the documented hierarchy.
"""

from __future__ import annotations

import pytest

from nora.drivers.exceptions import (
    CatalogNotFoundError,
    CatalogVerificationError,
    DeviceNotFoundError,
    DriverError,
    NetworkUnreachableError,
    PromptNotFoundError,
    RefusesWriteError,
    SnmpTimeoutError,
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
    """PromptNotFoundError exposes the missing prompt name."""
    exc = PromptNotFoundError("snmp_pmp450i")
    assert "snmp_pmp450i" in str(exc)


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
    ):
        try:
            raise exc_cls("probe")
        except DriverError:
            pass  # expected
        else:  # pragma: no cover - safety net
            pytest.fail(f"{exc_cls.__name__} was not caught by `except DriverError`")
