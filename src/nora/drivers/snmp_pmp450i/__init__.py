"""Cambium PMP 450i SNMP driver — public facade.

This subpackage owns the per-vendor driver boundary. Phase 3 will add
sibling subpackages for PTP 450/650, PMP switches, etc. The current
module re-exports the typed exception hierarchy, the public models,
and the `Pmp450iDriver` facade so callers can write::

    from nora.drivers.snmp_pmp450i import (
        Pmp450iDriver, RadioMetricsReport, Device, Inventory,
        DriverError, RefusesWriteError, DeviceNotFoundError,
        NetworkUnreachableError, SnmpTimeoutError,
        CatalogNotFoundError, CatalogVerificationError,
        PromptNotFoundError,
    )
"""

from __future__ import annotations

from nora.drivers.exceptions import (
    CatalogNotFoundError,
    CatalogVerificationError,
    DeviceNotFoundError,
    DriverError,
    NetworkUnreachableError,
    PromptNotFoundError,
    RefusesWriteError,
    SnmpTimeoutError,
)
from nora.drivers.inventory import Device, Inventory
from nora.drivers.snmp_pmp450i.client import SnmpClient
from nora.drivers.snmp_pmp450i.driver import (
    Pmp450iDriver,
    default_client_factory,
    nora_session_set_focus,
)
from nora.drivers.snmp_pmp450i.report import RadioMetricsReport
from nora.drivers.snmp_pmp450i.v2c import V2CClient, make_v2c_client
from nora.drivers.snmp_pmp450i.v3 import V3Client, make_v3_client

__all__ = [
    "DriverError",
    "RefusesWriteError",
    "DeviceNotFoundError",
    "NetworkUnreachableError",
    "SnmpTimeoutError",
    "CatalogNotFoundError",
    "CatalogVerificationError",
    "PromptNotFoundError",
    "Device",
    "Inventory",
    "SnmpClient",
    "V2CClient",
    "V3Client",
    "make_v2c_client",
    "make_v3_client",
    "RadioMetricsReport",
    "Pmp450iDriver",
    "default_client_factory",
    "nora_session_set_focus",
]
