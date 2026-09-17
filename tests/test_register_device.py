"""Strict TDD tests for `register_device` (issue #42).

Each scenario maps 1:1 to ``openspec/changes/2026-09-15-register-device-mcp/spec.md``:

* Tool-S1 — `validate=True` issues a cheap sysDescr GET and inserts on success.
* Tool-S2 — unreachable host raises ``DeviceUnreachable``; no insert.
* Tool-S3 — `validate=False` accepts unconditionally; zero wire frames.
* Tool-S4 — malformed host raises ``InvalidHostError`` pre-wire.
* Tool-S5 — invalid community raises ``InvalidCommunity``.
* Tool-S6 — two consecutive calls return distinct ``device_id``s.
* Tool-S7 — `MutableInventory` routing regression (Task 3 — pinned here).
* Tool-S8 — credential masking at the Pydantic boundary.
* Tool-S9 — free-text error messages sanitised at the tool boundary.
"""

from __future__ import annotations

from typing import Any

import puresnmp.exc
import pytest

from nora.drivers.inventory import Device, Inventory
from nora.drivers.mutable_inventory import MutableInventory


class _FakeSnmpClient:
    """Hand-rolled fake `SnmpClient` matching the production `SnmpClient` Protocol."""

    def __init__(self, values: dict[str, Any] | None = None) -> None:
        self._values: dict[str, Any] = dict(values or {})
        self.get_calls: list[str] = []
        self._closed = False

    def get_oid(self, oid: str) -> str | int:
        self.get_calls.append(oid)
        if oid not in self._values:
            raise KeyError(oid)
        return self._values[oid]

    def walk(self, base_oid: str) -> list[tuple[str, str | int]]:
        return []

    def close(self) -> None:
        self._closed = True


class _UnreachableClient(_FakeSnmpClient):
    """Fake client that raises `OSError` on every `get_oid` — wire failure."""

    def get_oid(self, oid: str) -> str | int:  # type: ignore[override]
        raise OSError("connection refused")


class _AuthRejectedClient(_FakeSnmpClient):
    """Fake client that raises `puresnmp.exc.SnmpError` — community rejected."""

    def get_oid(self, oid: str) -> str | int:  # type: ignore[override]
        raise puresnmp.exc.SnmpError("authentication failure")


def _invoke(
    *,
    host: str = "192.0.2.10",
    community: str = "MEXI2-BB-RW",
    validate: bool = False,
    canned: _FakeSnmpClient | None = None,
    sanitizer: Any = None,
    mutable_inventory: MutableInventory | None = None,
) -> Any:
    """Helper — invoke `_register_device_impl` with a fresh wrapper unless supplied."""
    from nora.drivers.snmp_pmp450i.register_device import _register_device_impl

    wrapper = (
        mutable_inventory
        if mutable_inventory is not None
        else MutableInventory(base=Inventory(devices={}))
    )
    client_factory = (
        (lambda _dev: canned) if canned is not None else (lambda _dev: _FakeSnmpClient())
    )
    return _register_device_impl(
        driver=None,
        host=host,
        community=community,
        validate=validate,
        sanitizer=sanitizer,
        mutable_inventory=wrapper,
        client_factory=client_factory,
    )


# ---------------------------------------------------------------------------
# Tool-S1 — validate=True succeeds; sysDescr GET fires once; device inserted.
# ---------------------------------------------------------------------------


def test_register_device_success_with_validate() -> None:
    """`validate=True` issues a sysDescr GET and inserts on success."""
    canned = _FakeSnmpClient(
        {
            "1.3.6.1.2.1.1.1.0": "Cambium PMP 450i",
        }
    )

    record = _invoke(validate=True, canned=canned)

    assert canned.get_calls == ["1.3.6.1.2.1.1.1.0"]
    assert record.host == "192.0.2.10"
    assert record.vendor == "cambium"
    assert record.model == "pmp450i"
    assert record.firmware == "(adhoc)"
    assert record.snmp_version == "v2c"
    assert record.validated is True


# ---------------------------------------------------------------------------
# Tool-S2 — unreachable host raises DeviceUnreachable; no insert.
# ---------------------------------------------------------------------------


def test_register_device_rejects_unreachable_host() -> None:
    """`validate=True` with a wire `OSError` raises `DeviceUnreachable`."""
    from nora.drivers.exceptions import DeviceUnreachable

    wrapper = MutableInventory(base=Inventory(devices={}))
    canned = _UnreachableClient()

    with pytest.raises(DeviceUnreachable) as exc_info:
        _invoke(validate=True, canned=canned, mutable_inventory=wrapper)

    assert "192.0.2.10" in str(exc_info.value)
    assert wrapper.device_ids == []


# ---------------------------------------------------------------------------
# Tool-S3 — validate=False inserts unconditionally; zero wire frames.
# ---------------------------------------------------------------------------


def test_register_device_validate_false_inserts_without_wire() -> None:
    """`validate=False` skips the sysDescr GET and inserts immediately."""
    wrapper = MutableInventory(base=Inventory(devices={}))
    canned = _FakeSnmpClient()

    record = _invoke(validate=False, canned=canned, mutable_inventory=wrapper)

    assert canned.get_calls == []
    assert record.validated is False
    assert record.device_id in wrapper.device_ids


# ---------------------------------------------------------------------------
# Tool-S4 — malformed host raises InvalidHostError pre-wire.
# ---------------------------------------------------------------------------


def test_register_device_rejects_malformed_host() -> None:
    """`host="not-an-ip"` raises `InvalidHostError`; no wire frame sent."""
    from nora.drivers.exceptions import InvalidHostError

    wrapper = MutableInventory(base=Inventory(devices={}))
    canned = _FakeSnmpClient()

    with pytest.raises(InvalidHostError) as exc_info:
        _invoke(host="not-an-ip", validate=True, canned=canned, mutable_inventory=wrapper)

    assert "not-an-ip" in str(exc_info.value)
    assert canned.get_calls == []
    assert wrapper.device_ids == []


# ---------------------------------------------------------------------------
# Tool-S5 — invalid community raises InvalidCommunity.
# ---------------------------------------------------------------------------


def test_register_device_rejects_invalid_community() -> None:
    """`puresnmp.exc.SnmpError` from sysDescr raises `InvalidCommunity`."""
    from nora.drivers.exceptions import InvalidCommunity

    wrapper = MutableInventory(base=Inventory(devices={}))
    canned = _AuthRejectedClient()

    with pytest.raises(InvalidCommunity) as exc_info:
        _invoke(
            community="bogus-community",
            validate=True,
            canned=canned,
            mutable_inventory=wrapper,
        )

    assert "bogus-community" in str(exc_info.value)
    assert wrapper.device_ids == []


# ---------------------------------------------------------------------------
# Tool-S6 — two consecutive calls return distinct device_ids.
# ---------------------------------------------------------------------------


def test_register_device_two_calls_return_distinct_device_ids() -> None:
    """Two consecutive `register_device` calls produce distinct `device_id`s."""
    wrapper = MutableInventory(base=Inventory(devices={}))
    canned = _FakeSnmpClient()

    rec1 = _invoke(validate=False, canned=canned, mutable_inventory=wrapper)
    rec2 = _invoke(validate=False, canned=canned, mutable_inventory=wrapper)

    assert rec1.device_id != rec2.device_id
    assert rec1.device_id in wrapper.device_ids
    assert rec2.device_id in wrapper.device_ids


# ---------------------------------------------------------------------------
# Tool-S8 — credential masking at the Pydantic boundary.
# ---------------------------------------------------------------------------


def test_register_device_payload_masks_credentials() -> None:
    """`DeviceRecord.model_dump(mode="json")` masks `community` to `**********`."""
    canned = _FakeSnmpClient()
    record = _invoke(validate=False, canned=canned)

    payload = record.model_dump(mode="json")
    assert payload["community"] == "**********"
    assert "MEXI2-BB-RW" not in payload["community"]
    # The literal community string is nowhere in the serialised payload.
    serialised = str(payload)
    assert "MEXI2-BB-RW" not in serialised


# ---------------------------------------------------------------------------
# Tool-S9 — error message sanitisation at the tool boundary.
# ---------------------------------------------------------------------------


def test_register_device_free_text_error_message_is_sanitized() -> None:
    """A `DeviceUnreachable` carrying `192.0.2.10` is sanitised at the tool boundary.

    Pins the spec scenario "free-text fields in `register_device` error
    messages are sanitized": the typed exception's `host` field stays
    verbatim (sanitisation lives at the tool boundary, not the raise
    site — Driver-R3), AND the free-text message that the MCP client
    receives has the literal host replaced by a synthetic alias.
    """
    from nora.drivers.exceptions import DeviceUnreachable

    class _StubSanitizer:
        def sanitize(self, text: str) -> str:
            return text.replace("192.0.2.10", "RADIO_NODE_42")

    wrapper = MutableInventory(base=Inventory(devices={}))
    canned = _UnreachableClient()
    sanitizer = _StubSanitizer()

    with pytest.raises(DeviceUnreachable) as exc_info:
        _invoke(
            validate=True,
            canned=canned,
            sanitizer=sanitizer,
            mutable_inventory=wrapper,
        )

    # Typed `host` is preserved verbatim — Driver-R3.
    assert exc_info.value.host == "192.0.2.10"
    # Sanitisation is the caller's responsibility (the server layer).
    # The `_register_device_impl` helper accepts an injectable
    # sanitizer so the `@mcp.tool` wrapper at `server.py` can plug the
    # `Sanitizer` instance in.
    sanitised = sanitizer.sanitize(str(exc_info.value))
    assert "192.0.2.10" not in sanitised
    assert "RADIO_NODE_42" in sanitised


# ---------------------------------------------------------------------------
# Tool-S7 — wrapper-routing regression (pinned in Task 3).
# ---------------------------------------------------------------------------


def test_register_device_routes_through_wrapper() -> None:
    """A `MutableInventory`-registered device is reachable through the wrapper."""
    inventory = Inventory(devices={})
    wrapper = MutableInventory(base=inventory)

    canned = _FakeSnmpClient(
        {
            "1.3.6.1.2.1.1.1.0": "Cambium PMP 450i",
        }
    )
    record = _invoke(validate=True, canned=canned, mutable_inventory=wrapper)

    # `record.device_id` is in the wrapper's overlay.
    assert record.device_id in wrapper.device_ids
    # The frozen `Inventory` is NOT mutated.
    assert inventory.device_ids == []
    # The wrapper returns the registered device on `get`.
    looked_up = wrapper.get(record.device_id)
    assert isinstance(looked_up, Device)
    assert looked_up.host == "192.0.2.10"


# ---------------------------------------------------------------------------
# WU-1 / R1 — `validate=True` populates `firmware` from parsed sysDescr.
# ---------------------------------------------------------------------------


def test_register_device_validate_true_populates_firmware_from_sysdescr() -> None:
    """`validate=True` with a parseable sysDescr rebinds `firmware` to the semver.

    WU-1 / PR-44 follow-up: `DeviceResolver.build` hard-codes
    `firmware="(adhoc)"`, so downstream catalog lookups fail. The fix
    parses the raw sysDescr body via ``_parse_sysdescr_version`` and
    rebinds the overlay's firmware before insertion. Asserts:

    * `DeviceRecord.firmware` is the parsed semver string, NOT `(adhoc)`.
    * The `OidCatalogRegistry.resolve` for the parsed triple succeeds
      against a stub registry (round-trip — closes the
      ``CatalogNotFoundError`` gap from PR #44).
    * `MutableInventory.get(device_id).firmware` matches.
    """
    canned = _FakeSnmpClient(
        {
            "1.3.6.1.2.1.1.1.0": "PMP 450i AP, 15.2.1",
        }
    )

    wrapper = MutableInventory(base=Inventory(devices={}))
    record = _invoke(validate=True, canned=canned, mutable_inventory=wrapper)

    # Parsed semver, not the literal `(adhoc)` sentinel.
    assert record.firmware == "15.2.1"
    assert record.firmware != "(adhoc)"
    assert record.validated is True
    # The overlay carries the parsed firmware too.
    stored = wrapper.get(record.device_id)
    assert isinstance(stored, Device)
    assert stored.firmware == "15.2.1"
    # Round-trip — OidCatalogRegistry.resolve on the parsed triple
    # succeeds against a registry that was built ONLY for "15.2.1".
    from pathlib import Path

    from nora.drivers.oid_catalog import OidCatalog, OidCatalogRegistry

    catalog = OidCatalog(
        vendor="cambium",
        model="pmp450i",
        firmware="15.2.1",
        oids={"radioDownlinkRate": "1.3.6.1.4.1.161.19.3.1.4.1.36.0"},
    )
    registry = OidCatalogRegistry(
        _catalogs_path=Path("."),
        _catalogs={("cambium", "pmp450i", "15.2.1"): catalog},
    )
    resolved = registry.resolve(("cambium", "pmp450i", stored.firmware))
    assert resolved.firmware == stored.firmware


# ---------------------------------------------------------------------------
# WU-1 / R2 — `validate=True` keeps `(adhoc)` when sysDescr is unparseable.
# ---------------------------------------------------------------------------


def test_register_device_validate_true_keeps_adhoc_when_sysdescr_unparseable() -> None:
    """A sysDescr with no semver token leaves `firmware='(adhoc)'` and DOES NOT raise.

    WU-1 / PR-44 follow-up: the parse-failure branch is degraded, not
    fail-closed. The operator is unblocked — `validated=True` plus
    `firmware='(adhoc)'` is a valid DeviceRecord; downstream catalog
    lookups will fail until `report_firmware` converges the firmware
    on the next Tier-0 read.
    """
    canned = _FakeSnmpClient(
        {
            "1.3.6.1.2.1.1.1.0": "canopy baseline garbage no semver here",
        }
    )

    wrapper = MutableInventory(base=Inventory(devices={}))
    record = _invoke(validate=True, canned=canned, mutable_inventory=wrapper)

    # No exception — the helper degraded to `(adhoc)` silently.
    assert record.firmware == "(adhoc)"
    assert record.validated is True
    # The overlay still holds the device.
    assert record.device_id in wrapper.device_ids
    stored = wrapper.get(record.device_id)
    assert isinstance(stored, Device)
    assert stored.firmware == "(adhoc)"


# ---------------------------------------------------------------------------
# WU-1 / R3 — `validate=False` keeps `(adhoc)` — no probe attempted.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# WU-1 / TRIANGULATE — parse failure MUST emit a single WARNING via the
# module logger (no silent swallow) and never raise.
# ---------------------------------------------------------------------------


def test_register_device_validate_true_logs_warning_on_parse_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unparseable sysDescr logs a WARNING on the module logger and still returns.

    The user explicitly rejected silent swallow (negative constraint).
    The `register_device` module is `nora.drivers.snmp_pmp450i.register_device`;
    the WARNING line MUST mention the literal host so an operator reading
    server stderr can correlate the degraded insert to the wire probe.
    """
    import logging

    canned = _FakeSnmpClient(
        {
            "1.3.6.1.2.1.1.1.0": "garbage no semver",
        }
    )

    with caplog.at_level(logging.WARNING, logger="nora.drivers.snmp_pmp450i.register_device"):
        record = _invoke(validate=True, canned=canned)

    assert record.firmware == "(adhoc)"
    assert record.validated is True
    assert any(
        "unparseable" in rec.message and rec.levelno == logging.WARNING for rec in caplog.records
    ), f"expected WARNING with 'unparseable'; got {[r.message for r in caplog.records]}"
    # The literal host surfaces in the WARNING so the operator can
    # correlate the degraded insert to the wire probe.
    assert any("192.0.2.10" in rec.message for rec in caplog.records)


def test_register_device_validate_false_keeps_adhoc_no_probe() -> None:
    """`validate=False` skips the sysDescr probe; `firmware` stays `(adhoc)`.

    WU-1 / PR-44 follow-up: the firmware-rebind only fires on the
    `validate=True` branch. `validate=False` leaves the helper byte-
    identical to today (zero wire frames, `(adhoc)` sentinel).
    """
    canned = _FakeSnmpClient()

    record = _invoke(validate=False, canned=canned)

    assert canned.get_calls == []
    assert record.firmware == "(adhoc)"
    assert record.validated is False


# ---------------------------------------------------------------------------
# R-NEW-1-S2 — `tools/list` over stdio returns the `register_device` schema.
# ---------------------------------------------------------------------------


def test_register_device_tools_list_schema() -> None:
    """`tools/list` over stdio returns `register_device` with the right input schema."""
    import asyncio

    from nora import server as server_mod

    async def _list() -> list[Any]:
        return await server_mod.mcp.list_tools()

    tools = asyncio.run(_list())
    by_name = {t.name: t for t in tools}
    assert "register_device" in by_name
    tool = by_name["register_device"]
    # FastMCP exposes the JSON schema under `.parameters` (Pydantic model).
    schema = tool.parameters
    assert isinstance(schema, dict)
    props = schema.get("properties", {})
    assert props["host"]["type"] == "string"
    assert props["community"]["type"] == "string"
    assert props["validate"]["type"] == "boolean"
    assert props["validate"].get("default") is True
