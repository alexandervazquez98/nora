"""Tests for `Inventory.from_yaml` and `Device` model.

Maps Driver-R1 (typed Device) and Inventory contract from the design.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from nora.drivers.exceptions import DeviceNotFoundError
from nora.drivers.inventory import Device, Inventory


def _write_inventory(path: Path, devices: list[dict[str, Any]]) -> Path:
    path.write_text(yaml.safe_dump({"devices": devices}))
    return path


# ---------------------------------------------------------------------------
# Device model — credential gating
# ---------------------------------------------------------------------------


def test_v2c_device_requires_community(tmp_path: Path) -> None:
    """`snmp_version=v2c` without `community` fails Pydantic validation."""
    with pytest.raises(ValueError):
        Device(
            device_id="ap-7400-01",
            vendor="cambium",
            model="pmp450i",
            firmware="15.2.1",
            host="192.0.2.10",
            snmp_version="v2c",
        )


def test_v3_device_requires_auth_password(tmp_path: Path) -> None:
    """`snmp_version=v3` without `auth_password` fails Pydantic validation."""
    with pytest.raises(ValueError):
        Device(
            device_id="ap-7400-01",
            vendor="cambium",
            model="pmp450i",
            firmware="15.2.1",
            host="192.0.2.10",
            snmp_version="v3",
            priv_password="change-me-priv",
        )


def test_v3_device_requires_priv_password(tmp_path: Path) -> None:
    """`snmp_version=v3` without `priv_password` fails Pydantic validation."""
    with pytest.raises(ValueError):
        Device(
            device_id="ap-7400-01",
            vendor="cambium",
            model="pmp450i",
            firmware="15.2.1",
            host="192.0.2.10",
            snmp_version="v3",
            auth_password="change-me-auth",
        )


def test_v2c_device_with_community_constructs(tmp_path: Path) -> None:
    """A complete v2c Device with community round-trips through the model."""
    dev = Device(
        device_id="ap-7400-01",
        vendor="cambium",
        model="pmp450i",
        firmware="15.2.1",
        host="192.0.2.10",
        snmp_version="v2c",
        community="change-me-community",
    )
    assert dev.device_id == "ap-7400-01"
    assert dev.firmware == "15.2.1"
    assert dev.community is not None
    assert dev.community.get_secret_value() == "change-me-community"


def test_v3_device_with_auth_and_priv_constructs(tmp_path: Path) -> None:
    """A complete v3 Device with auth + priv passwords round-trips."""
    dev = Device(
        device_id="ap-7400-01",
        vendor="cambium",
        model="pmp450i",
        firmware="15.2.1",
        host="192.0.2.10",
        snmp_version="v3",
        auth_password="change-me-auth",
        priv_password="change-me-priv",
    )
    assert dev.snmp_version == "v3"
    assert dev.auth_password is not None
    assert dev.priv_password is not None


# ---------------------------------------------------------------------------
# Inventory.from_yaml round-trip
# ---------------------------------------------------------------------------


def test_inventory_loads_single_device(tmp_path: Path) -> None:
    """`Inventory.from_yaml` returns an `Inventory` with one device."""
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
    inventory = Inventory.from_yaml(path)
    assert inventory.device_ids == ["ap-7400-01"]


def test_inventory_unknown_id_raises_device_not_found(tmp_path: Path) -> None:
    """`Inventory.get(unknown_id)` raises `DeviceNotFoundError`."""
    path = _write_inventory(tmp_path / "devices.yaml", [])
    inventory = Inventory.from_yaml(path)
    with pytest.raises(DeviceNotFoundError) as exc:
        inventory.get("missing-id")
    assert "missing-id" in str(exc.value)


def test_inventory_get_returns_typed_device(tmp_path: Path) -> None:
    """`Inventory.get(...)` returns a `Device` (not dict / Any)."""
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
    inventory = Inventory.from_yaml(path)
    dev = inventory.get("ap-7400-01")
    assert isinstance(dev, Device)
    assert dev.device_id == "ap-7400-01"


def test_inventory_loads_multiple_devices(tmp_path: Path) -> None:
    """Multiple devices in the YAML file all load."""
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
                "community": "change-me-1",
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
        ],
    )
    inventory = Inventory.from_yaml(path)
    assert set(inventory.device_ids) == {"ap-7400-01", "sm-7400-02"}
    dev_v3 = inventory.get("sm-7400-02")
    assert dev_v3.snmp_version == "v3"


def test_inventory_duplicate_id_raises(tmp_path: Path) -> None:
    """Two entries with the same `device_id` fail validation."""
    with pytest.raises(ValueError):
        _write_inventory(
            tmp_path / "devices.yaml",
            [
                {
                    "device_id": "dup",
                    "vendor": "cambium",
                    "model": "pmp450i",
                    "firmware": "15.2.1",
                    "host": "192.0.2.10",
                    "snmp_version": "v2c",
                    "community": "change-me",
                },
                {
                    "device_id": "dup",
                    "vendor": "cambium",
                    "model": "pmp450i",
                    "firmware": "15.2.1",
                    "host": "192.0.2.11",
                    "snmp_version": "v2c",
                    "community": "change-me",
                },
            ],
        )
        Inventory.from_yaml(tmp_path / "devices.yaml")
