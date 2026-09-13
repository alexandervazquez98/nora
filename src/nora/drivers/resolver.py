"""`DeviceResolver` — IP-direct resolution path for ad-hoc equipment.

Issue #14: pre-slice-1, an IP literal absent from `devices.yaml` raised
`DeviceNotFoundError` because `Inventory.from_yaml` was the only
resolution path. `DeviceResolver.build(host, snmp_version, creds)` fills
that gap:

* Returns a frozen `Device` (the same model the inventory consumes) so
  callers can hand it to any driver without conditional plumbing.
* `device_id` is the collision-safe stem
  ``f"adhoc-{host}-{secrets.token_hex(3)}"`` (3 bytes = 6 lowercase hex).
  Two callers hitting the same host in the same process get distinct
  stems; the entropy comes from `secrets.token_hex(3)` which is
  stronger than a counter because the OS RNG is the source.
* `SnmpCredentials` wraps every credential in `pydantic.SecretStr` so
  `repr(device)` and `device.model_dump()` mask the bytes (the test
  `test_secret_str_safety_no_plaintext_in_repr` pins that contract).

The resolver does NOT mutate `Settings.nora_devices_inventory_path`:
the test `test_build_does_not_open_devices_yaml` monkey-patches
`Path.read_text` to trip if the resolver opens the inventory file. The
resolver is pure — no file I/O, no DNS, no TCP connect.
"""

from __future__ import annotations

import secrets

from pydantic import BaseModel, ConfigDict, SecretStr

from nora.drivers.inventory import Device, SnmpVersion

__all__ = ["DeviceResolver", "SnmpCredentials"]


class SnmpCredentials(BaseModel):
    """Version-flexible credential bundle for `DeviceResolver.build`.

    Only `SecretStr` is accepted for any credential field — passing a
    plain `str` raises `pydantic.ValidationError`. The Device model
    validator at `build(...)` time enforces the version-specific shape
    (v2c needs `community`; v3 needs `auth_password` + `priv_password`).
    """

    model_config = ConfigDict(frozen=True)

    community: SecretStr | None = None
    auth_password: SecretStr | None = None
    priv_password: SecretStr | None = None


class DeviceResolver:
    """Pure factory — never touches the inventory file or the network.

    Methods are static by design: there is no instance state to leak
    between calls, and the test suite can call `build(...)` without
    first constructing a `DeviceResolver` instance.
    """

    @staticmethod
    def build(
        host: str,
        snmp_version: SnmpVersion,
        creds: SnmpCredentials,
    ) -> Device:
        """Return a frozen `Device` whose `device_id` is collision-safe.

        See module docstring for the full contract. The Device model's
        intrinsic validator surfaces version-specific credential
        requirements (`v2c → community`; `v3 → auth + priv`).
        """
        device_id = f"adhoc-{host}-{secrets.token_hex(3)}"
        # Firmware is a placeholder — `report_firmware()` queries the
        # agent's sysDescr at runtime for the real version, so the
        # inventory-loaded `Device.firmware` field is not authoritative
        # for the ad-hoc path. The literal `(adhoc)` keeps the field
        # parseable while making the path obvious to an operator.
        device_kwargs: dict[str, object] = {
            "device_id": device_id,
            "vendor": "cambium",
            "model": "pmp450i",
            "firmware": "(adhoc)",
            "host": host,
            "snmp_version": snmp_version,
        }
        if snmp_version == "v2c":
            device_kwargs["community"] = creds.community
        else:
            device_kwargs["auth_password"] = creds.auth_password
            device_kwargs["priv_password"] = creds.priv_password
        return Device(**device_kwargs)
