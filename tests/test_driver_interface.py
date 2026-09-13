"""Tests for `DeviceDriverInterface` Protocol + IP-direct resolution path.

PR 1 — slice 1 of the PMP 450i production surface. These tests pin the
public contract that:

* `DeviceResolver.build(host, snmp_version, creds)` returns a frozen
  `Device` whose `device_id` is the collision-safe stem
  ``f"adhoc-{host}-{secrets.token_hex(3)}"``.
* `Pmp450iSnmpDriver` adapts `Pmp450iDriver` AND exposes
  `report_firmware(device_id) -> packaging.version.Version`.
* The inventory path stays back-compatible (regression suite stays green).

Named tests for slice 1:

* ``test_ip_direct_resolution_builds_ephemeral_device``
* ``test_report_firmware_returns_typed_version``
* ``test_inventory_path_still_works_no_regression``

Zero-Leakage: only TEST-NET-1 (``192.0.2.x``) host literals; no real
IPs, hostnames, serials, or credentials.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
import yaml
from packaging.version import Version
from pydantic import SecretStr

from nora.drivers.inventory import Device, Inventory
from nora.drivers.oid_catalog import OidCatalog, OidCatalogRegistry

# ---------------------------------------------------------------------------
# Helpers (mirror those used in tests/test_driver_snmp_pmp450i.py)
# ---------------------------------------------------------------------------


def _build_inventory(tmp_path: Path) -> Inventory:
    """Hermetic inventory with one v2c AP and one v3 SM."""
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
    """Catalog with the 6 required OIDs."""
    catalog = OidCatalog(
        vendor="cambium",
        model="pmp450i",
        firmware="15.2.1",
        oids={
            "radioDownlinkRate": "1.3.6.1.4.1.161.19.3.1.1.1.0",
            "radioUplinkRate": "1.3.6.1.4.1.161.19.3.1.1.2.0",
            "signalStrengthRx": "1.3.6.1.4.1.161.19.3.1.1.3.0",
            "signalStrengthTx": "1.3.6.1.4.1.161.19.3.1.1.4.0",
            "ssr": "1.3.6.1.4.1.161.19.3.1.1.5.0",
            "modulationMode": "1.3.6.1.4.1.161.19.3.1.1.6.0",
        },
    )
    return OidCatalogRegistry(
        _catalogs_path=Path("."),
        _catalogs={("cambium", "pmp450i", "15.2.1"): catalog},
    )


# ---------------------------------------------------------------------------
# Named test #1 — ip_direct_resolution_builds_ephemeral_device
# ---------------------------------------------------------------------------


def test_ip_direct_resolution_builds_ephemeral_device() -> None:
    """`DeviceResolver.build("192.0.2.10", "v3", creds)` returns a frozen Device.

    The returned `device_id` is ``"adhoc-<host>-<6-hex>"`` so two
    concurrent calls against the same host never share a stem.
    """
    from nora.drivers.resolver import DeviceResolver, SnmpCredentials

    creds = SnmpCredentials(
        auth_password=SecretStr("test-auth-only"),
        priv_password=SecretStr("test-priv-only"),
    )
    device = DeviceResolver.build("192.0.2.10", "v3", creds)

    assert isinstance(device, Device)
    assert device.device_id.startswith("adhoc-192.0.2.10-")
    # The trailing 6-hex comes from `secrets.token_hex(3)` (3 bytes).
    suffix = device.device_id.split("-")[-1]
    assert len(suffix) == 6 and all(c in "0123456789abcdef" for c in suffix), (
        f"entropy suffix must be 6 lowercase hex chars, got {suffix!r}"
    )
    assert device.host == "192.0.2.10"
    assert device.snmp_version == "v3"
    assert device.vendor == "cambium"
    assert device.model == "pmp450i"


def test_stem_collisions_resolved_by_entropy_suffix() -> None:
    """Two consecutive `DeviceResolver.build(host, ...)` calls yield different stems.

    Triangulation: same host + same creds → two distinct device_ids, both
    sharing the host prefix. This pins the entropy-suffix derivation
    (not just a counter) so two callers in the same process never collide.
    """
    from nora.drivers.resolver import DeviceResolver, SnmpCredentials

    creds = SnmpCredentials(
        community=SecretStr("change-me"),
    )
    first = DeviceResolver.build("192.0.2.10", "v2c", creds)
    second = DeviceResolver.build("192.0.2.10", "v2c", creds)

    assert first.device_id != second.device_id
    assert first.device_id.startswith("adhoc-192.0.2.10-")
    assert second.device_id.startswith("adhoc-192.0.2.10-")
    # Same host / vendor / model / snmp_version — only the entropy differs.
    assert first.host == second.host
    assert first.snmp_version == second.snmp_version


def test_adhoc_device_is_frozen(tmp_path: Path) -> None:
    """`DeviceResolver.build(...)` returns a frozen `Device`.

    The same invariant as inventory-loaded Devices — a Pydantic `frozen`
    model — so neither the driver nor the caller can mutate the resolved
    `Device` mid-flight.
    """
    from nora.drivers.resolver import DeviceResolver, SnmpCredentials

    creds = SnmpCredentials(
        community=SecretStr("change-me"),
    )
    device = DeviceResolver.build("192.0.2.10", "v2c", creds)

    with pytest.raises(Exception):
        # Pydantic frozen models raise ValidationError (TypeError pre-2.x).
        device.host = "192.0.2.99"  # type: ignore[misc]


def test_adhoc_v3_requires_auth_and_priv() -> None:
    """v3 credentials REQUIRE both auth_password and priv_password.

    Pin the Device model's intrinsic validator: a half-supplied v3
    credential set must NOT silently degrade to v2c.
    """
    from nora.drivers.resolver import DeviceResolver, SnmpCredentials

    creds = SnmpCredentials(
        auth_password=SecretStr("only-auth"),
        # priv_password intentionally absent.
    )
    with pytest.raises(ValueError):
        DeviceResolver.build("192.0.2.10", "v3", creds)


# ---------------------------------------------------------------------------
# Named test #2 — report_firmware_returns_typed_version
# ---------------------------------------------------------------------------


def test_report_firmware_returns_typed_version(tmp_path: Path) -> None:
    """`Pmp450iSnmpDriver.report_firmware("ap-7400-01")` returns `Version("15.3.0")`.

    The agent advertises the firmware via sysDescr; the driver
    parses the string and returns a `packaging.version.Version` — the
    upstream of `OidCatalogRegistry.resolve(...)` per ADR #17 P2.
    """
    from nora.drivers.snmp_pmp450i import Pmp450iSnmpDriver
    from nora.drivers.snmp_pmp450i.report import RadioMetricsReport

    inv = _build_inventory(tmp_path)
    registry = _build_catalog()

    canned_report = RadioMetricsReport(
        device_id="ap-7400-01",
        fetched_at=datetime(2026, 1, 1, 12, 0, 0),
        firmware="15.2.1",
        radio_dl_rate_bps=54000000,
        radio_ul_rate_bps=21000000,
        rx_signal_dbm=-58,
        tx_signal_dbm=23,
        ssr=75,
        modulation="256QAM",
    )

    class _SysDescrClient:
        """Fake SnmpClient that returns a Cambium sysDescr string."""

        def __init__(self, sys_descr: str) -> None:
            self._sys_descr = sys_descr

        def get_oid(self, oid: str) -> str | int:
            if oid == "1.3.6.1.2.1.1.1.0":
                return self._sys_descr
            # Mirror the same canned values used elsewhere for the
            # radio-metrics OIDs so the driver is exercised too.
            return {
                "1.3.6.1.4.1.161.19.3.1.1.1.0": str(canned_report.radio_dl_rate_bps),
                "1.3.6.1.4.1.161.19.3.1.1.2.0": str(canned_report.radio_ul_rate_bps),
                "1.3.6.1.4.1.161.19.3.1.1.3.0": str(canned_report.rx_signal_dbm),
                "1.3.6.1.4.1.161.19.3.1.1.4.0": str(canned_report.tx_signal_dbm),
                "1.3.6.1.4.1.161.19.3.1.1.5.0": str(canned_report.ssr),
                "1.3.6.1.4.1.161.19.3.1.1.6.0": canned_report.modulation,
            }[oid]

        def walk(self, base_oid: str) -> list[tuple[str, str | int]]:
            return []

        def close(self) -> None:
            return None

    sys_descr = "Cambium Networks PMP 450i Access Point. Software Version 15.3.0 build 1"
    driver = Pmp450iSnmpDriver(
        inventory=inv,
        catalog_registry=registry,
        client_factory=lambda d: _SysDescrClient(sys_descr),
    )

    version = driver.report_firmware("ap-7400-01")

    assert isinstance(version, Version)
    assert version == Version("15.3.0")


def test_report_firmware_via_snmprec_style_sysdescr(tmp_path: Path) -> None:
    """Triangulation: alternative sysDescr wording (e.g. comma-separated).

    Different Cambium firmware revisions emit slightly different
    sysDescr strings; the parser must still extract the semver.
    """
    from nora.drivers.snmp_pmp450i import Pmp450iSnmpDriver

    inv = _build_inventory(tmp_path)
    registry = _build_catalog()

    sys_descr = "PMP 450i AP, 15.2.1"

    class _Client:
        def get_oid(self, oid: str) -> str | int:
            return sys_descr if oid == "1.3.6.1.2.1.1.1.0" else ""

        def walk(self, base_oid: str) -> list[tuple[str, str | int]]:
            return []

        def close(self) -> None:
            return None

    driver = Pmp450iSnmpDriver(
        inventory=inv,
        catalog_registry=registry,
        client_factory=lambda d: _Client(),
    )
    version = driver.report_firmware("ap-7400-01")
    assert version == Version("15.2.1")


# ---------------------------------------------------------------------------
# Protocol runtime check
# ---------------------------------------------------------------------------


def test_protocol_runtime_check_passes_for_pmp450i_snmp_driver(tmp_path: Path) -> None:
    """`isinstance(driver, DeviceDriverInterface)` is True for `Pmp450iSnmpDriver`."""
    from nora.drivers.interface import DeviceDriverInterface
    from nora.drivers.snmp_pmp450i import Pmp450iSnmpDriver
    from nora.drivers.snmp_pmp450i.client import SnmpClient

    inv = _build_inventory(tmp_path)
    registry = _build_catalog()

    fake_client = mock.MagicMock(spec=SnmpClient)
    driver = Pmp450iSnmpDriver(
        inventory=inv,
        catalog_registry=registry,
        client_factory=lambda d: fake_client,
    )
    assert isinstance(driver, DeviceDriverInterface)
    # Every declared method is callable.
    for method in (
        "fetch_radio_metrics",
        "fetch_ap_summary",
        "fetch_frame_utilization",
        "fetch_sm_table",
        "fetch_sm_detailed_diagnostics",
        "report_firmware",
    ):
        assert callable(getattr(driver, method, None)), f"{method!r} not callable"


def test_a_missing_method_fails_the_runtime_check(tmp_path: Path) -> None:
    """`isinstance(stub, DeviceDriverInterface)` is False for a partial stub.

    The runtime_checkable Protocol uses the structural shape; a class
    missing one method MUST fail the check so the boot sequence aborts.
    """
    from nora.drivers.interface import DeviceDriverInterface

    class _PartialStub:
        """Has four of the six Protocol methods — missing two on purpose."""

        def fetch_radio_metrics(self, device_id: str) -> Any:  # pragma: no cover - stub
            return None

        def fetch_ap_summary(self, device_id: str) -> Any:  # pragma: no cover
            return None

        def fetch_frame_utilization(self, device_id: str) -> Any:  # pragma: no cover
            return None

        # Missing: fetch_sm_table, fetch_sm_detailed_diagnostics, report_firmware.

    assert isinstance(_PartialStub(), DeviceDriverInterface) is False


def test_pmp450i_snmp_driver_subclass_of_pmp450i_driver(tmp_path: Path) -> None:
    """`Pmp450iSnmpDriver` is a subclass of `Pmp450iDriver` (public surface preserved).

    Slice-1 invariant: any code that holds a `Pmp450iDriver` reference
    can be handed a `Pmp450iSnmpDriver` without breaking.
    """
    from nora.drivers.snmp_pmp450i import Pmp450iDriver, Pmp450iSnmpDriver

    inv = _build_inventory(tmp_path)
    registry = _build_catalog()
    driver = Pmp450iSnmpDriver(
        inventory=inv,
        catalog_registry=registry,
        client_factory=lambda d: mock.MagicMock(),
    )
    assert isinstance(driver, Pmp450iDriver)


# ---------------------------------------------------------------------------
# Back-compat regression — inventory_path_still_works_no_regression
# ---------------------------------------------------------------------------


def test_inventory_path_still_works_no_regression(tmp_path: Path) -> None:
    """The existing `Pmp450iDriver.fetch_radio_metrics(...)` path is preserved.

    Triangulation: with the new Protocol seam in place, the legacy
    inventory fetch still returns a typed `RadioMetricsReport`.
    """
    from nora.drivers.snmp_pmp450i import Pmp450iDriver, RadioMetricsReport
    from nora.drivers.snmp_pmp450i.client import SnmpClient

    inv = _build_inventory(tmp_path)
    registry = _build_catalog()
    fake_client = mock.MagicMock(spec=SnmpClient)
    fake_client.get_oid.side_effect = lambda oid: {
        "1.3.6.1.4.1.161.19.3.1.1.1.0": "54000000",
        "1.3.6.1.4.1.161.19.3.1.1.2.0": "21000000",
        "1.3.6.1.4.1.161.19.3.1.1.3.0": "-58",
        "1.3.6.1.4.1.161.19.3.1.1.4.0": "23",
        "1.3.6.1.4.1.161.19.3.1.1.5.0": "75",
        "1.3.6.1.4.1.161.19.3.1.1.6.0": "256QAM",
    }[oid]

    # Use Pmp450iDriver (not the new SnmpDriver) to exercise the
    # legacy entry point; Pmp450iSnmpDriver.test_report_firmware_returns_typed_version
    # already covers the new class.
    driver = Pmp450iDriver(
        inventory=inv, catalog_registry=registry, client_factory=lambda d: fake_client
    )
    report = driver.fetch_radio_metrics("ap-7400-01")
    assert isinstance(report, RadioMetricsReport)
    assert report.device_id == "ap-7400-01"
    assert report.firmware == "15.2.1"
    assert report.modulation == "256QAM"


# ---------------------------------------------------------------------------
# Slice 2/3 stub coverage — `Pmp450iSnmpDriver.fetch_*` raises NotImplementedError
# pointing at the slice that completes each method.
# ---------------------------------------------------------------------------


def test_registry_get_driver_raises_when_uninitialised() -> None:
    """`get_driver()` raises `DriverError` when the singleton is None.

    The boot-fatal contract: every code path that touches `get_driver()`
    without first calling `set_driver(...)` from `__main__.main()`
    surfaces a typed exception instead of returning a confusing None.
    """
    # Save/restore so the global singleton doesn't leak between tests.
    import nora.drivers.registry as _registry
    from nora.drivers import DriverError, get_driver, set_driver

    saved = _registry._driver
    _registry._driver = None
    try:
        with pytest.raises(DriverError) as exc_info:
            get_driver()
        assert "not initialised" in str(exc_info.value)
    finally:
        _registry._driver = saved
        # Defensive: re-bind the module-level reference too in case
        # get_driver is bound via the `from nora.drivers import ...`
        # import path (mirrors what existing callers do).
        set_driver(saved)


def test_registry_accepts_both_driver_classes(tmp_path: Path) -> None:
    """`set_driver(...)` accepts either `Pmp450iDriver` or `Pmp450iSnmpDriver`.

    The union-typed singleton lets existing boot sequences pass the
    legacy class while new boot sequences pass the Protocol adapter.
    """
    from nora.drivers import set_driver
    from nora.drivers.snmp_pmp450i import Pmp450iDriver, Pmp450iSnmpDriver
    from nora.drivers.snmp_pmp450i.client import SnmpClient

    inv = _build_inventory(tmp_path)
    registry = _build_catalog()

    legacy = Pmp450iDriver(
        inventory=inv,
        catalog_registry=registry,
        client_factory=lambda d: mock.MagicMock(spec=SnmpClient),
    )
    new = Pmp450iSnmpDriver(
        inventory=inv,
        catalog_registry=registry,
        client_factory=lambda d: mock.MagicMock(spec=SnmpClient),
    )

    # Both must be accepted; the union widening pins the contract.
    set_driver(legacy)
    set_driver(new)
    set_driver(None)


# ---------------------------------------------------------------------------
# Slice 2/3 stub coverage — `Pmp450iSnmpDriver.fetch_*` raises NotImplementedError
# pointing at the slice that completes each method.
#
# PR 3 (slice 3) shipped both ``fetch_sm_table`` and
# ``fetch_sm_detailed_diagnostics`` so the parametrize list is now
# empty. Slice 4 (``spectrum`` + ``migrate``) ships in PR 4 — its
# stubs land there, not here.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method_name",
    [
        # Slice 2 (PR 2) — implemented; no longer stub.
        # "fetch_ap_summary",
        # "fetch_frame_utilization",
        # Slice 3 (PR 3) — implemented; no longer stub.
        # "fetch_sm_table",
        # "fetch_sm_detailed_diagnostics",
    ],
)
def test_slice_3_stubs_raise_not_implemented(method_name: str, tmp_path: Path) -> None:
    """The remaining `fetch_*` stubs raise `NotImplementedError` with slice pointers.

    PR 1 ships the Protocol surface; slice 2 (`fetch_ap_summary` +
    `fetch_frame_utilization`) lands in PR 2 — see
    ``tests/test_snmp_summaries.py``. Slice 3 (`fetch_sm_table` +
    `fetch_sm_detailed_diagnostics`) lands in PR 3 — see
    ``tests/test_snmp_subscribers.py``. The remaining stubs (slice 4
    `spectrum` + `migrate`) ship in PR 4.
    """
    from nora.drivers.snmp_pmp450i import Pmp450iSnmpDriver

    inv = _build_inventory(tmp_path)
    registry = _build_catalog()
    driver = Pmp450iSnmpDriver(
        inventory=inv,
        catalog_registry=registry,
        client_factory=lambda d: mock.MagicMock(),
    )

    with pytest.raises(NotImplementedError) as exc_info:
        getattr(driver, method_name)("ap-7400-01")
    message = str(exc_info.value)
    assert "PR " in message, (
        f"{method_name!r} stub must reference the PR that lands it; got: {message!r}"
    )
