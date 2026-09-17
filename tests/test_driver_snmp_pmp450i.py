"""Tests for `Pmp450iDriver.fetch_radio_metrics` + RadioMetricsReport.

Maps Driver-R1 (typed protocol support), R3 (typed return), R5 (typed
error mapping), R6 (within-call OID cache). The SessionJournal focus
side-effect (Driver-R4) was eliminated as part of the `nora-mcp-thin-split`
cut — see `test_fetch_radio_metrics_calls_set_focus_first` for the
regression guard.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from nora.drivers.exceptions import (
    DeviceNotFoundError,
    NetworkUnreachableError,
    SnmpTimeoutError,
)
from nora.drivers.inventory import Device, Inventory
from nora.drivers.oid_catalog import OidCatalog, OidCatalogRegistry
from nora.drivers.snmp_pmp450i import (
    RadioMetricsReport,
)
from nora.drivers.snmp_pmp450i import driver as _driver_mod
from nora.drivers.snmp_pmp450i.client import SnmpClient
from nora.drivers.snmp_pmp450i.driver import Pmp450iDriver

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_inventory(tmp_path: Path) -> Inventory:
    """Build a hermetic inventory with one v2c and one v3 device."""
    import yaml

    payload = {
        "devices": [
            {
                "device_id": "ap-7400-01",
                "vendor": "cambium",
                "model": "pmp450i",
                "firmware": "15.2.1",
                "host": "192.0.2.10",
                "snmp_version": "v2c",
                "community": "change-me-v2c",
            },
            {
                "device_id": "sm-7400-02",
                "vendor": "cambium",
                "model": "pmp450i",
                "firmware": "15.2.1",
                "host": "192.0.2.11",
                "snmp_version": "v3",
                "auth_password": "change-me-auth",
                "priv_password": "change-me-priv",
            },
        ]
    }
    inv_path = tmp_path / "devices.yaml"
    inv_path.write_text(yaml.safe_dump(payload))
    return Inventory.from_yaml(inv_path)


def _build_catalog() -> OidCatalogRegistry:
    """Build a registry with the 6 required OIDs as dotted strings.

    Issue #35 — verified WHISP-APS-MIB positions under whispLinkTable
    (.3.1.4.1) for the radio-metrics leaf columns. The placeholder
    sequential positions used in the original v1 seed (.3.1.1.X) were
    never reachable against a real Cambium PMP 450i radio.
    """
    catalog = OidCatalog(
        vendor="cambium",
        model="pmp450i",
        firmware="15.2.1",
        oids={
            "radioDownlinkRate": "1.3.6.1.4.1.161.19.3.1.4.1.36.0",
            "radioUplinkRate": "1.3.6.1.4.1.161.19.3.1.4.1.38.0",
            "signalStrengthRx": "1.3.6.1.4.1.161.19.3.1.4.1.34.0",
            "signalStrengthTx": "1.3.6.1.4.1.161.19.3.1.4.1.89.0",
            "ssr": "1.3.6.1.4.1.161.19.3.1.4.1.86.0",
            "modulationMode": "1.3.6.1.4.1.161.19.3.1.4.1.40.0",
        },
    )
    return OidCatalogRegistry(
        _catalogs_path=Path("."),
        _catalogs={("cambium", "pmp450i", "15.2.1"): catalog},
    )


def _fake_values() -> dict[str, str]:
    return {
        "1.3.6.1.4.1.161.19.3.1.4.1.36.0": "54000000",
        "1.3.6.1.4.1.161.19.3.1.4.1.38.0": "21000000",
        "1.3.6.1.4.1.161.19.3.1.4.1.34.0": "-58",
        "1.3.6.1.4.1.161.19.3.1.4.1.89.0": "23",
        "1.3.6.1.4.1.161.19.3.1.4.1.86.0": "75",
        "1.3.6.1.4.1.161.19.3.1.4.1.40.0": "256QAM",
    }


# ---------------------------------------------------------------------------
# R1 — Protocol support (typed v2c + v3 fetch)
# ---------------------------------------------------------------------------


def test_v2c_fetch_returns_typed_report(tmp_path: Path) -> None:
    """v2c fetch folds SNMP GET results into a typed `RadioMetricsReport`."""
    inv = _build_inventory(tmp_path)
    registry = _build_catalog()
    fake_client = mock.MagicMock(spec=SnmpClient)
    fake_client.get_oid.side_effect = lambda oid: _fake_values()[oid]
    driver = Pmp450iDriver(
        inventory=inv, catalog_registry=registry, client_factory=lambda d: fake_client
    )

    report = driver.fetch_radio_metrics("ap-7400-01")
    assert isinstance(report, RadioMetricsReport)
    assert report.device_id == "ap-7400-01"
    assert report.firmware == "15.2.1"
    assert report.radio_dl_rate_bps == 54000000
    assert report.radio_ul_rate_bps == 21000000
    assert report.rx_signal_dbm == -58
    assert report.tx_signal_dbm == 23
    assert report.ssr == 75
    assert report.modulation == "256QAM"
    # No dict / Any field — every field is a typed scalar.
    assert isinstance(report.fetched_at, datetime)


def test_v3_fetch_returns_typed_report(tmp_path: Path) -> None:
    """v3 fetch with auth + priv returns a typed report."""
    inv = _build_inventory(tmp_path)
    registry = _build_catalog()
    fake_client = mock.MagicMock(spec=SnmpClient)
    fake_client.get_oid.side_effect = lambda oid: _fake_values()[oid]
    factory = mock.MagicMock(return_value=fake_client)
    driver = Pmp450iDriver(inventory=inv, catalog_registry=registry, client_factory=factory)

    report = driver.fetch_radio_metrics("sm-7400-02")
    assert isinstance(report, RadioMetricsReport)
    factory.assert_called_once()
    called_device = factory.call_args[0][0]
    assert called_device.snmp_version == "v3"


# ---------------------------------------------------------------------------
# R3 — Strictly typed return (no dict / Any field)
# ---------------------------------------------------------------------------


def test_report_has_no_dict_or_any_field() -> None:
    """The `RadioMetricsReport` model exposes no `dict` / `Any` fields."""
    from typing import Any

    annotations = RadioMetricsReport.model_fields
    for name, field in annotations.items():
        if field.annotation is dict or field.annotation is Any:
            pytest.fail(f"RadioMetricsReport.{name} is dict or Any — must be a typed scalar")


# ---------------------------------------------------------------------------
# R5 — Failure surfaces typed errors
# ---------------------------------------------------------------------------


def test_unknown_device_id_raises_device_not_found(tmp_path: Path) -> None:
    """An unknown `device_id` raises `DeviceNotFoundError`."""
    inv = _build_inventory(tmp_path)
    registry = _build_catalog()
    driver = Pmp450iDriver(
        inventory=inv, catalog_registry=registry, client_factory=lambda d: mock.MagicMock()
    )
    with pytest.raises(DeviceNotFoundError):
        driver.fetch_radio_metrics("unknown")


def test_network_unreachable_raises_typed_error(tmp_path: Path) -> None:
    """A closed agent port surfaces `NetworkUnreachableError`."""
    inv = _build_inventory(tmp_path)
    registry = _build_catalog()

    fake_client = mock.MagicMock(spec=SnmpClient)
    fake_client.get_oid.side_effect = ConnectionRefusedError("closed")
    driver = Pmp450iDriver(
        inventory=inv, catalog_registry=registry, client_factory=lambda d: fake_client
    )

    with pytest.raises(NetworkUnreachableError):
        driver.fetch_radio_metrics("ap-7400-01")


def test_snmp_timeout_raises_typed_error(tmp_path: Path) -> None:
    """An unresponsive agent surfaces `SnmpTimeoutError`."""
    import socket

    inv = _build_inventory(tmp_path)
    registry = _build_catalog()

    fake_client = mock.MagicMock(spec=SnmpClient)
    fake_client.get_oid.side_effect = socket.timeout("agent took too long")
    driver = Pmp450iDriver(
        inventory=inv, catalog_registry=registry, client_factory=lambda d: fake_client
    )

    with pytest.raises(SnmpTimeoutError):
        driver.fetch_radio_metrics("ap-7400-01")


def test_malformed_oid_value_raises_typed_error(tmp_path: Path) -> None:
    """A non-numeric value on a numeric OID surfaces `NetworkUnreachableError`
    (the driver treats both as wire-level faults).
    """
    inv = _build_inventory(tmp_path)
    registry = _build_catalog()
    fake_client = mock.MagicMock(spec=SnmpClient)
    values = _fake_values()
    # Issue #35: the verified radioDownlinkRate position is
    # whispLinkTable (.3.1.4.1.36), not the placeholder sequential
    # .3.1.1.1.
    values["1.3.6.1.4.1.161.19.3.1.4.1.36.0"] = "not-a-number"
    fake_client.get_oid.side_effect = lambda oid: values[oid]
    driver = Pmp450iDriver(
        inventory=inv, catalog_registry=registry, client_factory=lambda d: fake_client
    )
    with pytest.raises((NetworkUnreachableError, SnmpTimeoutError, ValueError)):
        driver.fetch_radio_metrics("ap-7400-01")


# ---------------------------------------------------------------------------
# R6 — OID caching within a single call
# ---------------------------------------------------------------------------


def test_repeated_oid_lookup_within_call_is_cached(tmp_path: Path) -> None:
    """Within one tool call, repeated lookups reuse the same wire GET."""
    inv = _build_inventory(tmp_path)
    registry = _build_catalog()
    fake_client = mock.MagicMock(spec=SnmpClient)
    fake_client.get_oid.side_effect = lambda oid: _fake_values()[oid]
    driver = Pmp450iDriver(
        inventory=inv, catalog_registry=registry, client_factory=lambda d: fake_client
    )

    # Spy on the internal _get_oid path: count how many wire GETs were emitted.
    counter = {"calls": 0}
    original_get_oid = fake_client.get_oid.side_effect

    def _counting(oid: str) -> str:
        counter["calls"] += 1
        return original_get_oid(oid)

    fake_client.get_oid.side_effect = _counting

    report = driver.fetch_radio_metrics("ap-7400-01")

    # Six required OIDs; exactly six wire GETs (no duplicates).
    assert counter["calls"] == 6
    assert isinstance(report, RadioMetricsReport)


# ---------------------------------------------------------------------------
# R4 — Focus binding (calls set_focus first)
# ---------------------------------------------------------------------------


def test_fetch_radio_metrics_calls_set_focus_first(tmp_path: Path) -> None:  # noqa: ARG001
    """`fetch_radio_metrics` MUST NOT call `nora_session_set_focus` after the thin split.

    The SessionJournal was eliminated entirely (locked decision 3 + 5).
    The driver is now a pure typed fetch with no focus side-effect.
    This test pins the absence: any future re-introduction of the focus
    call must break this test and trigger a discussion.
    """
    import ast

    src = Path(_driver_mod.__file__).read_text()
    tree = ast.parse(src)
    offenders: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            # `nora_session_set_focus(...)` direct call.
            if isinstance(func, ast.Name) and func.id == "nora_session_set_focus":
                offenders.append((src.splitlines()[node.lineno - 1], node.lineno))
            # `nora_session_set_focus(...)` attribute call (e.g. inside a wrapper).
            if isinstance(func, ast.Attribute) and func.attr == "nora_session_set_focus":
                offenders.append((src.splitlines()[node.lineno - 1], node.lineno))
    assert offenders == [], f"driver.py must not reference nora_session_set_focus; got {offenders}"

    # Runtime assertion: even if a patched symbol exists in scope, the
    # driver MUST NOT call it.
    inv = _build_inventory(Path("/tmp"))
    registry = _build_catalog()
    fake_client = mock.MagicMock(spec=SnmpClient)
    fake_client.get_oid.side_effect = lambda oid: _fake_values()[oid]

    focus_calls: list[str] = []

    def _set_focus(device_id: str) -> dict[str, Any]:
        focus_calls.append(device_id)
        return {"focus_device_id": device_id, "devices_reviewed": [device_id]}

    # Even when the symbol exists in the module (it shouldn't), the
    # driver must not invoke it.
    with mock.patch.object(_driver_mod, "nora_session_set_focus", _set_focus, create=True):
        driver = Pmp450iDriver(
            inventory=inv, catalog_registry=registry, client_factory=lambda d: fake_client
        )
        driver.fetch_radio_metrics("ap-7400-01")

    assert focus_calls == [], (
        f"fetch_radio_metrics must not invoke the focus callable; calls: {focus_calls}"
    )


# ---------------------------------------------------------------------------
# WU-1 / R1 — `report_firmware` converges `(adhoc)` → parsed semver.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# WU-1 / R1 — `report_firmware` converges `(adhoc)` → parsed semver.
# ---------------------------------------------------------------------------


def test_report_firmware_converges_adhoc_to_parsed_semver(tmp_path: Path) -> None:
    """`report_firmware` on a `(adhoc)` device rebinds the overlay firmware.

    WU-1 / PR-44 follow-up: after `register_device` (which inserts
    with `(adhoc)`), a later Tier-0 `report_firmware` call observes
    the parsed semver from the agent's sysDescr AND converges the
    overlay entry so subsequent `OidCatalogRegistry.resolve(...)`
    lookups succeed. Asserts:

    * the return value of `report_firmware` is the typed `Version`.
    * the overlay `Device.firmware` is rebinded post-call.
    * the `MutableInventory` write seam (`update_firmware`) is the
      mechanism — not a private bypass.
    """
    from packaging.version import Version

    from nora.drivers.mutable_inventory import MutableInventory
    from nora.drivers.snmp_pmp450i.driver import Pmp450iSnmpDriver

    base = _build_inventory(tmp_path)
    wrapper = MutableInventory(base=base)

    # Register an ad-hoc `(adhoc)` device — emulates the
    # `register_device` MCP tool's insert path.
    adhoc_device_id = "adhoc-192-0-2-99-aabbcc"
    wrapper.register(
        Device(  # type: ignore[call-arg]
            device_id=adhoc_device_id,
            vendor="cambium",
            model="pmp450i",
            firmware="(adhoc)",
            host="192.0.2.99",
            snmp_version="v2c",
            community="change-me",
        )
    )
    assert wrapper.get(adhoc_device_id).firmware == "(adhoc)"

    # `report_firmware` reads sysDescr; we stub the wire layer.
    fake_client = mock.MagicMock(spec=SnmpClient)
    fake_client.get_oid.return_value = "PMP 450i AP, 16.1.0"
    factory = mock.MagicMock(return_value=fake_client)
    driver = Pmp450iSnmpDriver(
        inventory=wrapper,
        catalog_registry=_build_catalog(),
        client_factory=factory,
    )

    parsed = driver.report_firmware(adhoc_device_id)

    assert isinstance(parsed, Version)
    assert parsed == Version("16.1.0")
    # The overlay was updated — Tier-0 converged the firmware.
    assert wrapper.get(adhoc_device_id).firmware == "16.1.0"
    # Sanity: `MutableInventory.update_firmware` is the seam; the
    # driver MUST NOT have shadowed or replaced the Device object.
    assert wrapper.get(adhoc_device_id).host == "192.0.2.99"


# ---------------------------------------------------------------------------
# RadioMetricsReport.fold — typed contract
# ---------------------------------------------------------------------------


def test_fold_maps_oid_values_to_typed_fields(tmp_path: Path) -> None:
    """`fold` maps raw SNMP values into typed Pydantic fields."""
    inv = _build_inventory(tmp_path)
    device = inv.get("ap-7400-01")
    catalog = _build_catalog().resolve(("cambium", "pmp450i", "15.2.1"))
    report = RadioMetricsReport.fold(
        device=device,
        catalog=catalog,
        values=_fake_values(),
        fetched_at=datetime(2026, 1, 1, 12, 0, 0),
    )
    assert report.device_id == "ap-7400-01"
    assert report.firmware == "15.2.1"
    assert report.radio_dl_rate_bps == 54000000
    assert report.rx_signal_dbm == -58
    assert report.ssr == 75
    assert report.modulation == "256QAM"


# ---------------------------------------------------------------------------
# WU-1 / TRIANGULATE — convergence gate + frozen-inventory back-compat.
# ---------------------------------------------------------------------------


def test_report_firmware_does_not_touch_non_adhoc_firmware(tmp_path: Path) -> None:
    """Convergence only fires when `device.firmware == "(adhoc)"`.

    Back-compat guard: a YAML-loaded device (firmware already a real
    semver) MUST NOT be overwritten by `report_firmware` — the YAML is
    the boot-time source of truth and is authoritative. The parse
    result is still returned as a typed `Version`.
    """
    from packaging.version import Version

    from nora.drivers.snmp_pmp450i.driver import Pmp450iSnmpDriver

    inv = _build_inventory(tmp_path)  # ap-7400-01 has firmware='15.2.1'
    fake_client = mock.MagicMock(spec=SnmpClient)
    fake_client.get_oid.return_value = "PMP 450i AP, 16.1.0"
    factory = mock.MagicMock(return_value=fake_client)
    driver = Pmp450iSnmpDriver(
        inventory=inv,
        catalog_registry=_build_catalog(),
        client_factory=factory,
    )

    parsed = driver.report_firmware("ap-7400-01")

    assert parsed == Version("16.1.0")
    # Inventory is byte-identical — convergence did NOT fire.
    assert inv.get("ap-7400-01").firmware == "15.2.1"


def test_report_firmware_works_against_frozen_inventory(tmp_path: Path) -> None:
    """A frozen `Inventory` (no `update_firmware`) is left untouched.

    Back-compat guard: `report_firmware` was the original Tier-0 read
    method and pre-dates `MutableInventory`. The convergence branch
    is gated on `hasattr(inventory, "update_firmware")` so the
    legacy surface keeps working unchanged.
    """
    from packaging.version import Version

    from nora.drivers.snmp_pmp450i.driver import Pmp450iSnmpDriver

    inv = _build_inventory(tmp_path)  # plain frozen `Inventory`
    assert not hasattr(inv, "update_firmware")  # invariant for the gate
    fake_client = mock.MagicMock(spec=SnmpClient)
    fake_client.get_oid.return_value = "Cambium PMP 450i 15.2.1"
    factory = mock.MagicMock(return_value=fake_client)
    driver = Pmp450iSnmpDriver(
        inventory=inv,
        catalog_registry=_build_catalog(),
        client_factory=factory,
    )

    parsed = driver.report_firmware("ap-7400-01")

    assert parsed == Version("15.2.1")
    # Frozen inventory is byte-identical.
    assert inv.get("ap-7400-01").firmware == "15.2.1"
