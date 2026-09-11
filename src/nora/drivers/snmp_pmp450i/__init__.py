"""Cambium PMP 450i SNMP driver — public facade.

This subpackage owns the per-vendor driver boundary. Phase 3 will add
sibling subpackages for PTP 450/650, PMP switches, etc. The current
module only re-exports the typed exception hierarchy so import sites
can use ``from nora.drivers.snmp_pmp450i import RefusesWriteError``.
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

__all__ = [
    "DriverError",
    "RefusesWriteError",
    "DeviceNotFoundError",
    "NetworkUnreachableError",
    "SnmpTimeoutError",
    "CatalogNotFoundError",
    "CatalogVerificationError",
    "PromptNotFoundError",
]
