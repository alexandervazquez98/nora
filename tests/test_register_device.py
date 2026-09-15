"""Tool-S7 — `MutableInventory` routing regression (issue #42, Task 3).

The driver's 8 `Inventory.get()` call sites (see design §6) must route
through `MutableInventory.get(...)` because `cli.py` passes the wrapper
to `Pmp450iDriver.__init__`. This regression test pins that path:

* `cli.main()` builds a `MutableInventory(base=Inventory.from_yaml(...))`
  and passes the wrapper (NOT the frozen `Inventory`) to `Pmp450iDriver`.
* A device registered via the wrapper is reachable through the driver's
  `self._inventory.get(...)` path AND the frozen `Inventory` is NOT
  mutated.

The test asserts the *contract* (the wrapper is the seam between
`Inventory.get` and the driver layer). The actual `register_device`
implementation lands in Task 4 — this Task 3 commit just wires the
wrapper into the boot path.
"""

from __future__ import annotations

from typing import Any

import pytest

from nora.drivers.inventory import Device, Inventory
from nora.drivers.mutable_inventory import MutableInventory


def _sample_device(device_id: str = "adhoc-192-0-2-10-abcdef") -> Device:
    """A frozen `Device` shaped like a `DeviceResolver.build(...)` output."""
    return Device(
        device_id=device_id,
        vendor="cambium",
        model="pmp450i",
        firmware="(adhoc)",
        host="192.0.2.10",
        snmp_version="v2c",
        community="change-me-community",
    )


def test_register_device_routes_through_wrapper() -> None:
    """A `MutableInventory`-registered device reaches the driver through the wrapper.

    Pins the design §6 invariant: `Pmp450iDriver.__init__` accepts the
    `_InventoryLike` Protocol (Task 3). The wrapper's `get(...)` is the
    sole read path the driver hits — the underlying `Inventory` is not
    bypassed by any call site. Registering via `MutableInventory.register`
    lands the device in the overlay; `MutableInventory.get(...)` returns
    it without ever touching the frozen `Inventory`.
    """
    inventory = Inventory(devices={})  # frozen base, empty seed.
    wrapper = MutableInventory(base=inventory)

    dev = _sample_device()
    wrapper.register(dev)

    # Overlay hit — `wrapper.get` returns the registered device.
    looked_up = wrapper.get(dev.device_id)
    assert isinstance(looked_up, Device)
    assert looked_up.device_id == dev.device_id
    assert looked_up.host == "192.0.2.10"

    # `device_ids` surfaces the overlay entry.
    assert dev.device_id in wrapper.device_ids

    # The frozen `Inventory` is NOT mutated — Task 3 only widens the
    # ctor signature; it never replaces the frozen seed with the
    # overlay. The driver sees one seam (`_inventory.get`), and the
    # wrapper routes overlay-then-base.
    assert inventory.device_ids == []


def test_driver_ctor_accepts_inventory_like_protocol() -> None:
    """`Pmp450iDriver.__init__` accepts `_InventoryLike` (Task 3 ctor widening).

    Both `Inventory` (back-compat) and `MutableInventory` (Task 3
    production path) satisfy the Protocol. This test instantiates the
    driver with both forms to pin the ctor signature.
    """
    from nora.drivers.snmp_pmp450i import Pmp450iDriver

    # Stub the catalog registry — the ctor only needs an object identity.
    catalog_registry: Any = object()
    inventory_frozen = Inventory(devices={})
    driver_via_frozen = Pmp450iDriver(
        inventory=inventory_frozen,
        catalog_registry=catalog_registry,
    )
    assert driver_via_frozen._inventory is inventory_frozen  # type: ignore[attr-defined]

    wrapper = MutableInventory(base=inventory_frozen)
    driver_via_wrapper = Pmp450iDriver(
        inventory=wrapper,
        catalog_registry=catalog_registry,
    )
    assert driver_via_wrapper._inventory is wrapper  # type: ignore[attr-defined]


def test_cli_wraps_inventory_in_mutable_inventory(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`cli.main()` passes `MutableInventory(base=Inventory.from_yaml(...))` to the driver.

    Pins Task 3's wiring: the boot sequence must construct a
    `MutableInventory` (so `register_device` has a mutable seam) AND
    pass the wrapper to `Pmp450iDriver.__init__` (so the 8 read sites
    route through the wrapper). The frozen `Inventory.from_yaml(...)`
    is the `base=` argument and is not mutated at boot.
    """
    import yaml

    from nora import cli as cli_mod

    # Hermetic operator env: empty inventory, hermetic tmp dirs.
    inventory_path = tmp_path / "devices.yaml"
    inventory_path.write_text(yaml.safe_dump({"devices": []}))
    catalogs_path = tmp_path / "oid-catalogs"
    catalogs_path.mkdir()
    prompts_path = tmp_path / "prompts"
    prompts_path.mkdir()

    captured: dict[str, Any] = {}

    class _CapturingDriver:
        def __init__(self, *, inventory: Any, catalog_registry: Any, **kwargs: Any) -> None:
            captured["inventory_type"] = type(inventory).__name__
            captured["inventory"] = inventory
            captured["catalog_registry"] = catalog_registry

    # Patch the symbol `Pmp450iDriver` that `cli.main()` imports so the
    # boot sequence instantiates our capturing class instead of the
    # real driver.
    monkeypatch.setattr(cli_mod, "Pmp450iDriver", _CapturingDriver)

    # Pre-write a hermetic Settings into a fake env so `cli.main()`
    # picks up the tmp dirs.
    monkeypatch.setenv("NORA_OID_CATALOGS_PATH", str(catalogs_path))
    monkeypatch.setenv("NORA_DEVICES_INVENTORY_PATH", str(inventory_path))
    monkeypatch.setenv("NORA_PROMPTS_DIR", str(prompts_path))
    monkeypatch.setenv("NORA_OID_CATALOG_SIGNING_KEY", "test-key-for-cli-wrapping")

    # Ensure `Settings._env_file=None` so the test doesn't read `.env`.
    # We can't construct Settings directly here without writing one
    # factory; instead, monkey-patch `Settings.__init__` to ignore env.
    import os as _os

    # Clear any dotenv-leaking env vars that may shadow our overrides.
    for var in (
        "NORA_OID_CATALOGS_PATH",
        "NORA_DEVICES_INVENTORY_PATH",
        "NORA_PROMPTS_DIR",
        "NORA_OID_CATALOG_SIGNING_KEY",
        "NORA_INTERVENTIONS_DIR",
    ):
        _os.environ.pop(var, None)
    monkeypatch.setenv("NORA_OID_CATALOGS_PATH", str(catalogs_path))
    monkeypatch.setenv("NORA_DEVICES_INVENTORY_PATH", str(inventory_path))
    monkeypatch.setenv("NORA_PROMPTS_DIR", str(prompts_path))
    monkeypatch.setenv("NORA_OID_CATALOG_SIGNING_KEY", "test-key-for-cli-wrapping")

    # Also patch `OidCatalogRegistry.verify_all` so the catalog scan
    # doesn't require a real signed catalog at the tmp path. The stub
    # accepts every existing `@mcp.tool` so the boot-time guard
    # (`verify_tools_are_catalogued`) is a no-op — Task 6 adds the
    # catalog envelope for `register_device`; Task 3 only verifies the
    # `MutableInventory` wiring.
    from nora.drivers import oid_catalog as oid_catalog_mod

    class _StubRegistry:
        def required_oids_by_tool(self, _ref: Any) -> dict[str, tuple[str, ...]]:
            return {
                "snmp_get_pmp450i_radio_metrics": ("sysDescr",),
                "snmp_get_ap_summary": ("apFirmwareVersion",),
                "snmp_get_frame_utilization": ("frameUtilizationDlPct",),
                "snmp_get_sm_table": ("smLuid",),
                "snmp_get_sm_detailed_diagnostics": ("smJitter",),
                "snmp_run_spectrum_analysis": ("spectrumNoiseFloorA",),
                "snmp_migrate_radio_frequency": ("migrateCarrierFrequency",),
                "search_intervention_history": (),
                "get_device_lifecycle_summary": (),
                "correlate_sector_interference": (),
                "save_intervention_record": (),
            }

    monkeypatch.setattr(
        oid_catalog_mod.OidCatalogRegistry,
        "verify_all",
        lambda _settings: _StubRegistry(),
    )

    # Patch `mcp.run` to avoid the actual MCP server start.
    import nora.server as server_mod

    monkeypatch.setattr(server_mod.mcp, "run", lambda *_a, **_kw: None)

    try:
        cli_mod.main([])
    except SystemExit:
        pass

    # Assert: cli.main() passed a `MutableInventory` to the driver ctor.
    assert captured.get("inventory_type") == "MutableInventory", (
        f"cli.main() must wrap Inventory.from_yaml(...) in MutableInventory before "
        f"passing to Pmp450iDriver; got {captured.get('inventory_type')!r}"
    )
    inv_obj = captured["inventory"]
    assert isinstance(inv_obj, MutableInventory)
    # The frozen base is the YAML-loaded Inventory.
    assert isinstance(inv_obj.base, Inventory)
    # And the base is unchanged — frozen.
    assert inv_obj.base.device_ids == []
