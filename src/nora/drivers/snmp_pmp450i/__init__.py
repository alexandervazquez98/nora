"""Cambium PMP 450i SNMP driver — public facade.

This subpackage owns the per-vendor driver boundary. Phase 3 will add
sibling subpackages for PTP 450/650, PMP switches, etc. The current
module re-exports the typed exception hierarchy, the public models,
and the `Pmp450iDriver` / `Pmp450iSnmpDriver` facades so callers can
write::

    from nora.drivers.snmp_pmp450i import (
        Pmp450iDriver, Pmp450iSnmpDriver, RadioMetricsReport,
        Device, Inventory,
        DriverError, RefusesWriteError, DeviceNotFoundError,
        NetworkUnreachableError, SnmpTimeoutError,
        CatalogNotFoundError, CatalogVerificationError,
        PromptNotFoundError,
        AutonomousMutationRejected,
        MaintenanceWindowViolation,
        UncataloguedToolError,
    )
"""

from __future__ import annotations

from nora.drivers.exceptions import (
    AutonomousMutationRejected,
    CatalogNotFoundError,
    CatalogVerificationError,
    DeviceNotFoundError,
    DriverError,
    MaintenanceWindowViolation,
    NetworkUnreachableError,
    PromptNotFoundError,
    RefusesWriteError,
    SnmpTimeoutError,
    UncataloguedToolError,
)
from nora.drivers.inventory import Device, Inventory
from nora.drivers.snmp_pmp450i.client import SnmpClient, WritableSnmpClient
from nora.drivers.snmp_pmp450i.driver import (
    Pmp450iDriver,
    Pmp450iSnmpDriver,
    default_client_factory,
    default_writable_client_factory,
)
from nora.drivers.snmp_pmp450i.report import RadioMetricsReport
from nora.drivers.snmp_pmp450i.v2c import (
    V2CClient,
    WritableV2CClient,
    make_v2c_client,
    make_writable_v2c_client,
)
from nora.drivers.snmp_pmp450i.v3 import (
    V3Client,
    WritableV3Client,
    make_v3_client,
    make_writable_v3_client,
)

__all__ = [
    "DriverError",
    "RefusesWriteError",
    "DeviceNotFoundError",
    "NetworkUnreachableError",
    "SnmpTimeoutError",
    "CatalogNotFoundError",
    "CatalogVerificationError",
    "PromptNotFoundError",
    # Slice-1 stubs (raise sites in PR 4 + PR 5).
    "AutonomousMutationRejected",
    "MaintenanceWindowViolation",
    "UncataloguedToolError",
    "Device",
    "Inventory",
    "SnmpClient",
    "WritableSnmpClient",
    "V2CClient",
    "WritableV2CClient",
    "V3Client",
    "WritableV3Client",
    "make_v2c_client",
    "make_writable_v2c_client",
    "make_v3_client",
    "make_writable_v3_client",
    "RadioMetricsReport",
    "Pmp450iDriver",
    "Pmp450iSnmpDriver",
    "default_client_factory",
    "default_writable_client_factory",
]
