"""Device model + YAML inventory loader.

The inventory is the single source of truth for which physical / logical
devices the driver layer can talk to. Everything credential-sensitive is
held as `pydantic.SecretStr` so it never round-trips through plain
``str`` and never appears in a `repr`.

Contract:

* `Device` carries `vendor`, `model`, `firmware`, `host`, `port`, and
  the version-specific credentials (`community`, `auth_password`,
  `priv_password`).
* v2c requires `community`; v3 requires BOTH `auth_password` and
  `priv_password`. Pydantic-level validation surfaces this at load
  time (no surprise at SNMP wire time).
* `Inventory.from_yaml` returns an immutable inventory; `get(id)`
  raises `DeviceNotFoundError` for unknown ids.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    model_validator,
)

from nora.drivers.exceptions import DeviceNotFoundError

SnmpVersion = Literal["v2c", "v3"]


class Device(BaseModel):
    """A single physical / logical device with SNMP credentials."""

    model_config = ConfigDict(frozen=True)

    device_id: str
    vendor: Literal["cambium"]
    model: Literal["pmp450i"]
    firmware: str
    host: str
    port: int = 161
    snmp_version: SnmpVersion
    community: SecretStr | None = None
    auth_password: SecretStr | None = None
    priv_password: SecretStr | None = None

    @model_validator(mode="after")
    def _check_version_credentials(self) -> "Device":
        """v2c requires `community`; v3 requires `auth_password` + `priv_password`."""
        if self.snmp_version == "v2c":
            if self.community is None or self.community.get_secret_value() == "":
                raise ValueError(
                    f"Device {self.device_id!r}: v2c requires a non-empty community string"
                )
            # v3-only fields must NOT be set when v2c.
            if self.auth_password is not None or self.priv_password is not None:
                raise ValueError(
                    f"Device {self.device_id!r}: v3-only credentials require snmp_version='v3'"
                )
        else:  # v3
            if self.auth_password is None or self.auth_password.get_secret_value() == "":
                raise ValueError(
                    f"Device {self.device_id!r}: v3 requires a non-empty auth_password"
                )
            if self.priv_password is None or self.priv_password.get_secret_value() == "":
                raise ValueError(
                    f"Device {self.device_id!r}: v3 requires a non-empty priv_password"
                )
        return self


class Inventory(BaseModel):
    """In-memory map of device_id -> Device."""

    model_config = ConfigDict(frozen=True)

    devices: dict[str, Device] = Field(default_factory=dict)

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------

    @classmethod
    def from_yaml(cls, path: Path) -> "Inventory":
        """Load the inventory from `path`.

        The YAML file MUST have a top-level `devices:` key whose value is
        a list of device mappings. Duplicate `device_id` values raise
        `ValueError` (Pydantic catches the duplicate-key validation).
        """
        payload = yaml.safe_load(path.read_text()) or {}
        if not isinstance(payload, dict):
            raise ValueError(f"Inventory YAML root must be a mapping; got {type(payload).__name__}")
        raw_devices = payload.get("devices", [])
        if not isinstance(raw_devices, list):
            raise ValueError(
                f"Inventory YAML 'devices' key must be a list; got {type(raw_devices).__name__}"
            )
        devices: dict[str, Device] = {}
        for raw in raw_devices:
            if not isinstance(raw, dict):
                raise ValueError(f"Each device entry must be a mapping; got {type(raw).__name__}")
            dev = Device.model_validate(_coerce_secrets(raw))
            if dev.device_id in devices:
                raise ValueError(f"Duplicate device_id {dev.device_id!r} in inventory")
            devices[dev.device_id] = dev
        return cls(devices=devices)

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def get(self, device_id: str) -> Device:
        """Return the `Device` for `device_id` or raise `DeviceNotFoundError`."""
        try:
            return self.devices[device_id]
        except KeyError as exc:
            raise DeviceNotFoundError(device_id) from exc

    @property
    def device_ids(self) -> list[str]:
        return sorted(self.devices.keys())


def _coerce_secrets(raw: dict[str, Any]) -> dict[str, Any]:
    """Wrap credential-shaped fields in `SecretStr` for Pydantic."""
    out = dict(raw)
    for key in ("community", "auth_password", "priv_password"):
        if key in out and isinstance(out[key], str):
            out[key] = SecretStr(out[key])
    return out


__all__ = ["Device", "Inventory", "SnmpVersion"]
