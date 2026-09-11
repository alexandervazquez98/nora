"""Driver layer — read-only network telemetry for NORA.

Public surface is intentionally narrow: a small handful of typed
exceptions plus the public models are re-exported here so callers can
write ``from nora.drivers import DeviceNotFoundError`` without reaching
into a deep submodule. The implementation lives in:

    ``nora/drivers/exceptions.py``  — typed exception hierarchy.
    ``nora/drivers/inventory.py``   — `Device` + `Inventory`.
    ``nora/drivers/oid_catalog.py`` — `OidCatalog` + HMAC verify.
    ``nora/drivers/registry.py``    — module-level driver singleton.
    ``nora/drivers/snmp_pmp450i/``  — per-vendor SNMP driver.
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
from nora.drivers.registry import get_driver, set_driver

__all__ = [
    "DriverError",
    "RefusesWriteError",
    "DeviceNotFoundError",
    "NetworkUnreachableError",
    "SnmpTimeoutError",
    "CatalogNotFoundError",
    "CatalogVerificationError",
    "PromptNotFoundError",
    "get_driver",
    "set_driver",
]
