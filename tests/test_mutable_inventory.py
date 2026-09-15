"""Tests for the `MutableInventory` wrapper (issue #42).

The wrapper preserves `Inventory.frozen=True` by holding a separate
`dict[str, Device]` overlay and delegating `get` / `device_ids` reads
through to the underlying frozen `Inventory`. Mutators (`register` /
`unregister`) are guarded by `threading.Lock`; reads are lock-free.

Strict TDD per `openspec/config.yaml::testing.strict_tdd=true`: every
scenario from `spec.md` (`Inventory Mutation Contract`) maps to one
test here.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest
import yaml

from nora.drivers.exceptions import (
    DeviceNotFoundError,
    DuplicateDeviceError,
)
from nora.drivers.inventory import Device, Inventory


def _write_inventory(path: Path, devices: list[dict[str, Any]]) -> Path:
    path.write_text(yaml.safe_dump({"devices": devices}))
    return path


def _empty_seed() -> Inventory:
    """An `Inventory` with zero devices — the typical mutable seed."""
    return Inventory(devices={})


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


# ---------------------------------------------------------------------------
# Scenario Inv-1-S1 — register inserts a new device.
# ---------------------------------------------------------------------------


def test_register_inserts_new_device() -> None:
    """`MutableInventory.register(device)` makes the device reachable via `get`."""
    from nora.drivers.mutable_inventory import MutableInventory

    wrapper = MutableInventory(base=_empty_seed())
    dev = _sample_device()

    wrapper.register(dev)

    assert wrapper.get(dev.device_id) == dev
    assert dev.device_id in wrapper.device_ids


# ---------------------------------------------------------------------------
# Scenario Inv-1-S2 — register rejects duplicate device_id.
# ---------------------------------------------------------------------------


def test_register_rejects_duplicate_device_id() -> None:
    """A second `register(Device(device_id=existing))` raises `DuplicateDeviceError`.

    The original entry is preserved (no silent overwrite).
    """
    from nora.drivers.mutable_inventory import MutableInventory

    wrapper = MutableInventory(base=_empty_seed())
    first = _sample_device("ap-7400-01")
    wrapper.register(first)
    second = Device(
        device_id="ap-7400-01",
        vendor="cambium",
        model="pmp450i",
        firmware="(adhoc)",
        host="192.0.2.11",
        snmp_version="v2c",
        community="different-community",
    )

    with pytest.raises(DuplicateDeviceError) as exc_info:
        wrapper.register(second)

    assert "ap-7400-01" in str(exc_info.value)
    # The original entry is preserved — no silent overwrite.
    assert wrapper.get("ap-7400-01") == first


# ---------------------------------------------------------------------------
# Scenario Inv-1-S3 — unregister removes a runtime device.
# ---------------------------------------------------------------------------


def test_unregister_removes_runtime_device() -> None:
    """`unregister(device_id)` removes a runtime-registered device."""
    from nora.drivers.mutable_inventory import MutableInventory

    wrapper = MutableInventory(base=_empty_seed())
    dev = _sample_device("ap-7400-01")
    wrapper.register(dev)

    wrapper.unregister("ap-7400-01")

    with pytest.raises(DeviceNotFoundError):
        wrapper.get("ap-7400-01")
    assert "ap-7400-01" not in wrapper.device_ids


# ---------------------------------------------------------------------------
# Scenario Inv-1-S4 — unregister of an unknown id raises DeviceNotFoundError.
# ---------------------------------------------------------------------------


def test_unregister_unknown_id_raises_device_not_found() -> None:
    """`unregister("nonexistent")` raises `DeviceNotFoundError`."""
    from nora.drivers.mutable_inventory import MutableInventory

    wrapper = MutableInventory(base=_empty_seed())

    with pytest.raises(DeviceNotFoundError) as exc_info:
        wrapper.unregister("nonexistent")

    assert "nonexistent" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Scenario Inv-1-S5 — get delegates to base inventory for YAML-loaded entry.
# ---------------------------------------------------------------------------


def test_get_delegates_to_base_inventory_for_yaml_loaded_entry(tmp_path: Path) -> None:
    """`wrapper.get(id)` returns the YAML-loaded entry from the frozen base.

    The underlying `Inventory` is not mutated by the wrapper's read path;
    `Inventory.frozen=True` is preserved.
    """
    path = _write_inventory(
        tmp_path / "devices.yaml",
        [
            {
                "device_id": "ap-7400-01",
                "vendor": "cambium",
                "model": "pmp450i",
                "firmware": "15.2.1",
                "host": "192.0.2.10",
                "snmp_version": "v2c",
                "community": "change-me",
            }
        ],
    )
    base = Inventory.from_yaml(path)
    from nora.drivers.mutable_inventory import MutableInventory

    wrapper = MutableInventory(base=base)

    loaded = wrapper.get("ap-7400-01")

    assert isinstance(loaded, Device)
    assert loaded.device_id == "ap-7400-01"
    assert loaded.host == "192.0.2.10"
    # The base inventory is unchanged (frozen).
    assert base.device_ids == ["ap-7400-01"]
    # And the wrapper surfaces the same id list.
    assert wrapper.device_ids == ["ap-7400-01"]


# ---------------------------------------------------------------------------
# Concurrency — `register` / `unregister` are guarded by a lock.
# ---------------------------------------------------------------------------


def test_concurrent_registers_serialize_via_lock() -> None:
    """Concurrent `register` calls on distinct ids all land in the overlay.

    The `threading.Lock` inside `MutableInventory` serialises the mutator
    paths so two threads racing on `register` cannot corrupt the overlay
    dict (e.g. one thread's write lost between another's get + set).
    """
    from nora.drivers.mutable_inventory import MutableInventory

    wrapper = MutableInventory(base=_empty_seed())
    threads = 8
    barrier = threading.Barrier(threads)

    def _register(idx: int) -> None:
        dev = _sample_device(f"adhoc-192-0-2-{idx:02d}-aaaaaa")
        barrier.wait()  # maximise contention
        wrapper.register(dev)

    workers = [threading.Thread(target=_register, args=(i,)) for i in range(threads)]
    for t in workers:
        t.start()
    for t in workers:
        t.join()

    assert len(wrapper.device_ids) == threads
    for idx in range(threads):
        assert f"adhoc-192-0-2-{idx:02d}-aaaaaa" in wrapper.device_ids
