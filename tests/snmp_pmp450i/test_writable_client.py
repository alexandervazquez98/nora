"""Tests for the Driver-R2 carve-out: `WritableSnmpClient` Protocol + adapters.

Issue #62 (2026-09-19): the Cambium WHISP-BOX-MIBV2-MIB sweep
protocol requires three SET frames (duration + arm + start). The
read-only `SnmpClient` Protocol forbids write verbs; the carve-out
adds a sibling `WritableSnmpClient` Protocol that exposes one (and
only one) `set` verb. Two thin adapters (`WritableV2CClient`,
`WritableV3Client`) compose a read-only client and forward SET
through `puresnmp.PyWrapper.set`.

This module pins the contract:

1. `WritableSnmpClient` Protocol is runtime-checkable and a structural
   superset of `SnmpClient` (every read-only method still present,
   `set` added).
2. `WritableV2CClient` / `WritableV3Client` are NOT subclasses of
   `V2CClient` / `V3Client` (composition, not inheritance) but they
   forward every read-only call to the inner client and they expose
   `set` via the documented `puresnmp.PyWrapper.set` coroutine path.
3. The base `SnmpClient` Protocol stays read-only — `vars(SnmpClient)`
   still does NOT contain any of `_WRITE_VERB_SET`.
4. The driver's `writable_client_factory` seam picks v2c vs v3 from
   `device.snmp_version` exactly like the read-only
   `default_client_factory`.

These tests are hermetic — every dependency on the read-only
`V2CClient` / `V3Client` is replaced with a fake `FakeInner` that
records the method calls and the async coroutine plumbing.
"""

from __future__ import annotations

from typing import Any

import pytest

from nora.drivers.inventory import Device
from nora.drivers.snmp_pmp450i.client import SnmpClient, WritableSnmpClient
from nora.drivers.snmp_pmp450i.driver import (
    Pmp450iDriver,
    default_writable_client_factory,
)
from nora.drivers.snmp_pmp450i.v2c import WritableV2CClient
from nora.drivers.snmp_pmp450i.v3 import WritableV3Client

# Mirrors the carve-out constant in `test_driver_snmp450i_readonly.py`.
_WRITE_VERB_SET: frozenset[str] = frozenset({"set", "update", "setbulk", "bulk_set", "write"})


def _protocol_attrs(protocol: Any) -> frozenset[str]:
    """Return the declared callable attributes of a `typing.Protocol` class.

    `Protocol` records its method declarations on `__protocol_attrs__`
    (a frozenset of names) which includes inherited declarations from
    any base `Protocol` subclass. `vars(cls)` only shows the names
    declared directly on `cls`, so the superset check below uses this
    helper to walk the full MRO.
    """
    return getattr(protocol, "__protocol_attrs__", frozenset())


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeInner:
    """Minimal stand-in for `V2CClient` / `V3Client` exposed by the adapter.

    Records every method call so the test asserts the adapter forwards
    the read-only surface unchanged AND routes `set` through the
    documented `_call_async("set", ...)` plumbing. The `_call_async`
    plumbing is the canonical hook because the real `V2CClient` /
    `V3Client` keep their async-run helpers private (prefixed with
    `_`) and the adapters are documented as composing, not subclassing.
    """

    def __init__(self, return_value_for_set: Any = "ok") -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self._set_return = return_value_for_set

    def get_oid(self, oid: str) -> str | int:
        self.calls.append(("get_oid", (oid,)))
        return 42

    def walk(self, base_oid: str) -> list[tuple[str, str | int]]:
        self.calls.append(("walk", (base_oid,)))
        return [(base_oid, 1)]

    def close(self) -> None:
        self.calls.append(("close", ()))

    def _call_async(self, method_name: str, *args: Any) -> Any:
        # The real `_call_async` runs `asyncio.run(...)`; we record
        # the intent and return a sentinel without touching the event
        # loop. The adapter contract says the SET result is consumed
        # silently (`del result` in `WritableV2CClient.set`).
        self.calls.append(("_call_async", (method_name, *args)))
        if method_name == "set":
            return self._set_return
        raise AssertionError(f"_call_async called with unexpected method: {method_name!r}")


def _build_device(*, snmp_version: str) -> Device:
    """Build a minimal `Device` for the writable-factory dispatch tests."""
    if snmp_version == "v2c":
        from pydantic import SecretStr

        return Device(
            device_id="ap-1",
            vendor="cambium",
            model="pmp450i",
            firmware="25.0.1",
            host="192.0.2.10",
            port=161,
            snmp_version="v2c",
            community=SecretStr("public"),
        )
    from pydantic import SecretStr

    return Device(
        device_id="ap-2",
        vendor="cambium",
        model="pmp450i",
        firmware="25.0.1",
        host="192.0.2.20",
        port=161,
        snmp_version="v3",
        auth_password=SecretStr("authpass"),
        priv_password=SecretStr("privpass"),
    )


# ---------------------------------------------------------------------------
# Protocol surface — `WritableSnmpClient` is the documented carve-out
# ---------------------------------------------------------------------------


def test_writable_protocol_is_runtime_checkable() -> None:
    """`WritableSnmpClient` exposes `set` AND every read-only method."""
    write_set = _protocol_attrs(WritableSnmpClient)
    assert "set" in write_set, "WritableSnmpClient must declare `set`"
    read_only = {"get_oid", "walk", "close"}
    assert read_only <= write_set, (
        f"WritableSnmpClient must retain every read-only method; missing: {read_only - write_set}"
    )


def test_writable_protocol_is_superset_of_snmp_client() -> None:
    """Every method on `SnmpClient` is also on `WritableSnmpClient`."""
    snmp_methods = _protocol_attrs(SnmpClient)
    writable_methods = _protocol_attrs(WritableSnmpClient)
    assert snmp_methods <= writable_methods, (
        f"WritableSnmpClient must be a superset of SnmpClient; "
        f"missing: {snmp_methods - writable_methods}"
    )


def test_base_snmp_client_still_has_no_write_verbs() -> None:
    """Pin the read-only invariant on `SnmpClient` after the carve-out."""
    leaked = _protocol_attrs(SnmpClient) & _WRITE_VERB_SET
    assert leaked == set(), f"SnmpClient leaked write verb(s): {sorted(leaked)}"


def test_writable_protocol_exposes_only_one_write_verb() -> None:
    """Carve-out scope: exactly `set` and nothing else from `_WRITE_VERB_SET`."""
    leaked = _protocol_attrs(WritableSnmpClient) & _WRITE_VERB_SET
    assert leaked == {"set"}, (
        f"WritableSnmpClient must expose exactly `set` from the write blocklist; "
        f"got: {sorted(leaked)}"
    )


# ---------------------------------------------------------------------------
# Adapter composition — `WritableV2CClient` forwards read surface, owns `set`
# ---------------------------------------------------------------------------


def test_writable_v2c_client_forwards_get_oid() -> None:
    inner = _FakeInner()
    adapter = WritableV2CClient(client=inner)  # type: ignore[arg-type]

    result = adapter.get_oid("1.3.6.1.4.1.161.19.3.1.1.90.0")

    assert result == 42
    assert inner.calls == [("get_oid", ("1.3.6.1.4.1.161.19.3.1.1.90.0",))]


def test_writable_v2c_client_forwards_walk() -> None:
    inner = _FakeInner()
    adapter = WritableV2CClient(client=inner)  # type: ignore[arg-type]

    result = adapter.walk("1.3.6.1.4.1.161.19.3.2")

    assert result == [("1.3.6.1.4.1.161.19.3.2", 1)]
    assert inner.calls == [("walk", ("1.3.6.1.4.1.161.19.3.2",))]


def test_writable_v2c_client_forwards_close() -> None:
    inner = _FakeInner()
    adapter = WritableV2CClient(client=inner)  # type: ignore[arg-type]

    adapter.close()

    assert inner.calls == [("close", ())]


def test_writable_v2c_client_set_routes_through_call_async() -> None:
    """`set` invokes `_call_async("set", oid, value)` exactly once with x690 typing."""
    from x690.types import Integer

    inner = _FakeInner(return_value_for_set="assigned-varbind")
    adapter = WritableV2CClient(client=inner)  # type: ignore[arg-type]

    # Returns None per the Protocol contract.
    result = adapter.set("1.3.6.1.4.1.161.19.3.3.2.220.0", 15)

    assert result is None, "WritableV2CClient.set must return None per the Protocol contract"
    assert inner.calls == [
        ("_call_async", ("set", "1.3.6.1.4.1.161.19.3.3.2.220.0", Integer(15))),
    ]


def test_writable_v2c_client_accepts_string_value_for_octet_string() -> None:
    """`set` accepts `str` values too (OCTET STRING case)."""
    from x690.types import OctetString

    inner = _FakeInner()
    adapter = WritableV2CClient(client=inner)  # type: ignore[arg-type]

    adapter.set("1.3.6.1.4.1.161.19.3.3.2.221.0", "8")

    assert inner.calls == [
        ("_call_async", ("set", "1.3.6.1.4.1.161.19.3.3.2.221.0", OctetString(b"8"))),
    ]


def test_writable_v2c_client_rejects_both_client_and_device() -> None:
    """Construction with both `client` and `device` raises ValueError."""
    inner = _FakeInner()
    device = _build_device(snmp_version="v2c")

    with pytest.raises(ValueError, match="pass either `client` or `device`"):
        WritableV2CClient(client=inner, device=device)  # type: ignore[arg-type]


def test_writable_v2c_client_requires_client_or_device() -> None:
    """Construction with neither `client` nor `device` raises ValueError."""
    with pytest.raises(ValueError, match="requires either an existing V2CClient or a Device"):
        WritableV2CClient()  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Adapter composition — `WritableV3Client` mirrors V2C
# ---------------------------------------------------------------------------


def test_writable_v3_client_forwards_get_oid() -> None:
    inner = _FakeInner()
    adapter = WritableV3Client(client=inner)  # type: ignore[arg-type]

    result = adapter.get_oid("1.3.6.1.4.1.161.19.3.1.1.91.0")

    assert result == 42
    assert inner.calls == [("get_oid", ("1.3.6.1.4.1.161.19.3.1.1.91.0",))]


def test_writable_v3_client_set_routes_through_call_async() -> None:
    from x690.types import Integer

    inner = _FakeInner()
    adapter = WritableV3Client(client=inner)  # type: ignore[arg-type]

    result = adapter.set("1.3.6.1.4.1.161.19.3.3.2.220.0", 15)

    assert result is None
    assert inner.calls == [
        ("_call_async", ("set", "1.3.6.1.4.1.161.19.3.3.2.220.0", Integer(15))),
    ]


def test_writable_v3_client_rejects_both_client_and_device() -> None:
    inner = _FakeInner()
    device = _build_device(snmp_version="v3")

    with pytest.raises(ValueError, match="pass either `client` or `device`"):
        WritableV3Client(client=inner, device=device)  # type: ignore[arg-type]


def test_writable_v3_client_requires_client_or_device() -> None:
    with pytest.raises(ValueError, match="requires either an existing V3Client or a Device"):
        WritableV3Client()  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Driver integration — `writable_client_factory` seam
# ---------------------------------------------------------------------------


def test_default_writable_client_factory_returns_writable_v2c_for_v2c_device() -> None:
    device = _build_device(snmp_version="v2c")

    client = default_writable_client_factory(device)

    assert isinstance(client, WritableV2CClient)
    # Adapter must satisfy the WritableSnmpClient Protocol — runtime-checkable
    # so a plain `isinstance` works.
    assert isinstance(client, WritableSnmpClient)
    # And through the wider read-only Protocol too — composition.
    assert isinstance(client, SnmpClient)


def test_default_writable_client_factory_returns_writable_v3_for_v3_device() -> None:
    device = _build_device(snmp_version="v3")

    client = default_writable_client_factory(device)

    assert isinstance(client, WritableV3Client)
    assert isinstance(client, WritableSnmpClient)
    assert isinstance(client, SnmpClient)


def test_driver_stores_writable_client_factory_attribute() -> None:
    """`Pmp450iDriver` accepts and stores `writable_client_factory`."""
    device = _build_device(snmp_version="v2c")
    captured: list[Device] = []

    def fake_factory(dev: Device) -> WritableSnmpClient:
        captured.append(dev)
        return default_writable_client_factory(dev)

    # Minimal driver construction — no catalog registry, no inventory calls.
    # We don't trigger any wire fetch; the test only asserts the attribute
    # round-trips.
    driver = Pmp450iDriver(
        inventory=_StubInventory(),  # type: ignore[arg-type]
        catalog_registry=_StubRegistry(),  # type: ignore[arg-type]
        writable_client_factory=fake_factory,
    )

    factory = getattr(driver, "_writable_client_factory")
    assert factory is fake_factory, "driver must retain the injected writable_client_factory"

    # Smoke: invoking the captured factory must produce a valid Writable* adapter.
    built = factory(device)
    assert isinstance(built, WritableV2CClient)
    assert captured == [device]


def test_driver_writable_client_factory_default_picks_correct_family() -> None:
    """Default `writable_client_factory` dispatches v2c vs v3 by `snmp_version`."""
    driver = Pmp450iDriver(
        inventory=_StubInventory(),  # type: ignore[arg-type]
        catalog_registry=_StubRegistry(),  # type: ignore[arg-type]
    )
    factory = driver._writable_client_factory  # type: ignore[attr-defined]

    v2c_client = factory(_build_device(snmp_version="v2c"))
    v3_client = factory(_build_device(snmp_version="v3"))

    assert isinstance(v2c_client, WritableV2CClient)
    assert isinstance(v3_client, WritableV3Client)


# ---------------------------------------------------------------------------
# Tiny stubs — keep `Pmp450iDriver.__init__` happy without touching real data.
# ---------------------------------------------------------------------------


class _StubInventory:
    """Empty inventory stand-in. Only `__init__` is exercised here."""

    def get(self, device_id: str) -> Any:  # pragma: no cover - unused
        raise NotImplementedError


class _StubRegistry:
    """Empty catalog registry stand-in. Only `__init__` is exercised here."""

    def resolve(self, ref: tuple[str, str, str]) -> Any:  # pragma: no cover - unused
        raise NotImplementedError
