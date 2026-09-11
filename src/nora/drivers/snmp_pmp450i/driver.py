"""`Pmp450iDriver` — public facade.

Single read-only surface exposed to the MCP layer. Construction is via
dependency injection: the inventory, the catalog registry, and the
`client_factory` (used to choose v2c vs v3 from the device's
`snmp_version`) are passed in. Tests inject fakes; production wires
the registry into the boot sequence in `__main__.py`.

Flow (Driver-R1, R4, R5, R6):

    set_focus(device_id) -> resolve catalog -> get values for required OIDs ->
    fold into `RadioMetricsReport`. The within-call OID cache ensures
    repeated lookups for the same OID only emit one wire GET.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from functools import lru_cache
from typing import Callable, Protocol

from nora.drivers.exceptions import (
    NetworkUnreachableError,
    SnmpTimeoutError,
)
from nora.drivers.inventory import Device, Inventory
from nora.drivers.oid_catalog import REQUIRED_OIDS, OidCatalog, OidCatalogRegistry
from nora.drivers.snmp_pmp450i.client import SnmpClient
from nora.drivers.snmp_pmp450i.report import RadioMetricsReport

logger = logging.getLogger("nora.drivers.snmp_pmp450i")


# `nora_session_set_focus` is injected at runtime; we import lazily so
# tests that patch it at the module level still see the patched symbol.
def _default_set_focus(device_id: str) -> dict[str, object]:
    """Lazy import of the real `nora_session_set_focus` MCP tool.

    Returning a dict mirrors the MCP tool's documented payload shape;
    when a test patches this module attribute, the patched callable
    runs instead.
    """
    from nora.core.session_journal import get_journal

    journal = get_journal()
    state = journal.set_focus(device_id)
    return {
        "session_id": state.session_id,
        "focus_device_id": state.focus_device_id,
        "devices_reviewed": list(state.devices_reviewed),
    }


# Module-level alias so tests can monkey-patch `nora_session_set_focus`
# at the attribute level. `_default_set_focus` is the production path.
nora_session_set_focus = _default_set_focus


class _ClientFactory(Protocol):
    """Factory used to construct a client for `device`.

    Tests inject a callable returning a mock client; production passes
    a factory that picks `V2CClient` / `V3Client` from the device's
    `snmp_version`.
    """

    def __call__(self, device: Device) -> SnmpClient: ...


def default_client_factory(device: Device) -> SnmpClient:
    """Pick v2c or v3 from `device.snmp_version`."""
    from nora.drivers.snmp_pmp450i.v2c import make_v2c_client
    from nora.drivers.snmp_pmp450i.v3 import make_v3_client

    if device.snmp_version == "v2c":
        return make_v2c_client(device)
    return make_v3_client(device)


class Pmp450iDriver:
    """Public facade over the PMP 450i driver layer.

    Single `@mcp.tool` (`snmp_get_pmp450i_radio_metrics`) talks to one
    instance of this class — `DriverRegistry` provides the module-level
    singleton once `__main__.py` wires it at boot.
    """

    def __init__(
        self,
        *,
        inventory: Inventory,
        catalog_registry: OidCatalogRegistry,
        client_factory: Callable[[Device], SnmpClient] = default_client_factory,
    ) -> None:
        self._inventory = inventory
        self._catalog_registry = catalog_registry
        self._client_factory = client_factory

    def fetch_radio_metrics(self, device_id: str) -> RadioMetricsReport:
        """Fetch + fold a typed `RadioMetricsReport` for `device_id`.

        Steps:

        1. `nora_session_set_focus(device_id)` — Driver-R4.
        2. `Inventory.get(device_id)` — `DeviceNotFoundError` on miss.
        3. Resolve the catalog for `(vendor, model, firmware)` — the
           catalog registry was verified at boot, so this is a pure
           lookup.
        4. Open a client (v2c or v3) and fetch one value per required
           OID. A within-call LRU cache deduplicates identical OIDs
           (`Driver-R6`).
        5. `RadioMetricsReport.fold` produces the typed return.
        """
        nora_session_set_focus(device_id)

        device = self._inventory.get(device_id)
        catalog = self._catalog_registry.resolve((device.vendor, device.model, device.firmware))

        client = self._client_factory(device)
        try:
            values = self._fetch_all(client, catalog, device)
        finally:
            try:
                client.close()
            except Exception:  # pragma: no cover - close is best-effort
                pass

        report = RadioMetricsReport.fold(
            device=device,
            catalog=catalog,
            values=values,
            fetched_at=datetime.now(timezone.utc),
        )
        return report

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _fetch_all(
        self, client: SnmpClient, catalog: OidCatalog, device: Device
    ) -> "dict[str, str | int]":
        """Fetch every required OID exactly once (within this call).

        Wire-level failures are remapped to typed driver exceptions:

        * `TimeoutError`                               -> `SnmpTimeoutError`
        * `ConnectionRefusedError` / `OSError`        -> `NetworkUnreachableError`
        * `ValueError` (parse failure)                 -> `NetworkUnreachableError`
        """

        @lru_cache(maxsize=None)
        def _cached(oid: str) -> "str | int":
            return client.get_oid(oid)

        target = f"{device.host}:{device.port}"
        values: "dict[str, str | int]" = {}
        for oid_name in REQUIRED_OIDS:
            oid = catalog.oids[oid_name]
            try:
                values[oid] = _cached(oid)
            except TimeoutError as exc:
                raise SnmpTimeoutError(oid) from exc
            except OSError as exc:
                raise NetworkUnreachableError(target) from exc
            except ValueError as exc:
                raise NetworkUnreachableError(f"OID {oid}: {exc}") from exc
        return values


__all__ = ["Pmp450iDriver", "nora_session_set_focus", "default_client_factory"]
