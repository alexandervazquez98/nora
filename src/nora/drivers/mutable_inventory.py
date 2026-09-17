"""`MutableInventory` — runtime-mutable wrapper around frozen `Inventory`.

Issue #42 / change `2026-09-15-register-device-mcp`: the orchestrator
needs to register an ad-hoc PMP 450i radio whose IP is absent from
``data/devices.yaml``. The operator-facing contract forbids mutating
``Inventory`` directly (``frozen=True`` is a load-bearing invariant —
217 lines of ``tests/test_inventory.py`` plus the driver back-compat
suite rely on it).

The wrapper holds a separate ``dict[str, Device]`` overlay and a
read-through reference to a frozen ``Inventory``. Reads (`get`,
`device_ids`) consult the overlay first, then fall back to the base
inventory. Mutators (`register`, `unregister`) operate exclusively on
the overlay and are serialised by a `threading.Lock` so concurrent
`@mcp.tool` calls under FastMCP's async dispatch cannot corrupt it.

The Protocol surface (`_InventoryLike`) is the seam the driver ctor
exposes — both `Inventory` and `MutableInventory` satisfy it, so the
existing ``Inventory.get(...)`` call sites in
``snmp_pmp450i/{driver,summaries,subscribers,spectrum,migrate}.py``
route through the wrapper transparently when ``cli.py`` passes it to
``Pmp450iDriver``.
"""

from __future__ import annotations

import threading
from typing import Protocol

from nora.drivers.exceptions import DeviceNotFoundError, DuplicateDeviceError
from nora.drivers.inventory import Device, Inventory


class _InventoryLike(Protocol):
    """Structural type for the `Pmp450iDriver.__init__` ctor argument.

    Both `Inventory` (frozen, YAML-loaded) and `MutableInventory`
    (overlay) satisfy this Protocol — the back-compat suite
    (`tests/test_inventory.py`) keeps passing because the driver's
    eight ``self._inventory.get(...)`` read sites don't need to change.
    """

    def get(self, device_id: str) -> Device: ...
    @property
    def device_ids(self) -> list[str]: ...


class MutableInventory(_InventoryLike):
    """Read-through wrapper around a frozen `Inventory` with a mutable overlay.

    The base inventory stays immutable (read-only access). The overlay
    is a separate ``dict[str, Device]`` for runtime-registered devices
    (`register_device` MCP tool). Mutators take a `threading.Lock` so
    concurrent register/unregister calls serialise; reads are
    lock-free (Python's GIL plus the immutable base make this safe).

    The class is *not* a `pydantic.BaseModel` — it carries no field
    validation (devices flow through `Inventory` or `DeviceResolver`
    before insertion). A plain class keeps the surface tight and
    avoids the Pydantic v2 frozen/non-frozen dance.
    """

    def __init__(self, base: Inventory) -> None:
        self._base = base
        self._overlay: dict[str, Device] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Read API — Protocol-mandated, lock-free.
    # ------------------------------------------------------------------

    def get(self, device_id: str) -> Device:
        """Return the `Device` for `device_id` or raise `DeviceNotFoundError`.

        Overlay hits take priority over base hits. A miss on both
        surfaces raises `DeviceNotFoundError(device_id)` verbatim.
        """
        try:
            return self._overlay[device_id]
        except KeyError:
            pass
        try:
            return self._base.get(device_id)
        except DeviceNotFoundError as exc:
            raise DeviceNotFoundError(device_id) from exc

    @property
    def device_ids(self) -> list[str]:
        """Return every `device_id` (overlay + base), sorted."""
        combined: set[str] = {*self._overlay, *self._base.device_ids}
        return sorted(combined)

    # ------------------------------------------------------------------
    # Mutators — guarded by `_lock`.
    # ------------------------------------------------------------------

    def register(self, device: Device) -> None:
        """Insert `device` into the overlay.

        Raises `DuplicateDeviceError` if `device.device_id` already
        exists in the overlay OR the base inventory — a runtime
        registration must not shadow an existing YAML-loaded entry
        (a deliberate operator decision in `data/devices.yaml`).
        """
        with self._lock:
            if device.device_id in self._overlay:
                raise DuplicateDeviceError(device.device_id)
            if device.device_id in self._base.device_ids:
                raise DuplicateDeviceError(device.device_id)
            self._overlay[device.device_id] = device

    def unregister(self, device_id: str) -> None:
        """Remove `device_id` from the overlay.

        Raises `DeviceNotFoundError` if `device_id` is absent from the
        overlay. Base-inventory ids are immutable — the wrapper cannot
        unregister YAML-loaded entries (an operator must edit
        `data/devices.yaml` and restart to remove those).
        """
        with self._lock:
            if device_id not in self._overlay:
                raise DeviceNotFoundError(device_id)
            del self._overlay[device_id]

    def update_firmware(self, device_id: str, firmware: str) -> None:
        """Update the firmware string on an overlay-registered device.

        WU-1 / PR-44 follow-up: a guarded write seam that lets
        ``Pmp450iSnmpDriver.report_firmware`` converge an ``(adhoc)``
        placeholder to the parsed semver after the device was inserted
        by ``register_device``. The base inventory stays immutable —
        the YAML-loaded ``Device.firmware`` is signed at boot and is
        authoritative; an operator must edit ``data/devices.yaml`` and
        restart to change it.

        Behaviour:

        * Guarded by :attr:`_lock` (same semantics as :meth:`register` /
          :meth:`unregister`).
        * ``device_id`` must be in :attr:`_overlay`; otherwise
          :class:`nora.drivers.exceptions.DeviceNotFoundError` is raised
          verbatim — base-inventory ids are intentionally rejected so
          the contract mirrors ``unregister``.
        * The ``Device`` Pydantic model is ``frozen=True``; we
          re-construct via ``model_copy(update={"firmware": firmware})``
          so the public surface is identical.

        Parameters
        ----------
        device_id
            The runtime-registered device whose firmware to rebind.
        firmware
            The new firmware string (semver-shaped, e.g. ``"16.1.0"``).
            Not validated here — the caller is responsible for
            supplying a value ``OidCatalogRegistry.resolve`` accepts.
        """
        with self._lock:
            current = self._overlay.get(device_id)
            if current is None:
                raise DeviceNotFoundError(device_id)
            self._overlay[device_id] = current.model_copy(update={"firmware": firmware})

    # ------------------------------------------------------------------
    # Escape hatch — tests + cli boot path need it.
    # ------------------------------------------------------------------

    @property
    def base(self) -> Inventory:
        """Return the wrapped frozen `Inventory` (read-only)."""
        return self._base


__all__ = ["MutableInventory"]
