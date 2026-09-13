"""Driver registry — module-level singleton for the PMP 450i driver.

`__main__.py` calls `set_driver(driver)` after the boot sequence
loads the inventory + verifies the catalog. The MCP tool calls
`get_driver()` to fetch the singleton; tests inject a fake via
`set_driver(fake)`.

A `None` singleton surfaces as `DriverError("driver not initialised")`
when `get_driver()` is called before `set_driver(...)` — this matches
the boot-fatal contract from `__main__.py`.

Slice 1 widens the singleton's static type to `Pmp450iSnmpDriver`
(the adapter that implements `DeviceDriverInterface`). The original
`Pmp450iDriver` is still acceptable (it satisfies the narrower API
`get_driver` callers expect), so every existing boot sequence and
test stays green.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from nora.drivers.exceptions import DriverError

if TYPE_CHECKING:
    from nora.drivers.snmp_pmp450i import Pmp450iDriver, Pmp450iSnmpDriver

logger = logging.getLogger("nora.drivers.registry")


_driver: "Pmp450iSnmpDriver | Pmp450iDriver | None" = None


def set_driver(driver: "Pmp450iSnmpDriver | Pmp450iDriver | None") -> None:
    """Inject (or clear) the module-level driver singleton.

    Used by `__main__.py` at boot and by tests via `monkeypatch`.
    Accepts either the new `Pmp450iSnmpDriver` (slice 1 onwards) or
    the legacy `Pmp450iDriver` — both are valid because the new
    class subclasses the old one. The union keeps existing boot
    sequences green while enabling new code to inject the adapter.
    """
    global _driver
    _driver = driver
    if driver is None:
        logger.debug("driver registry cleared")
    else:
        logger.debug("driver registry injected: %s", type(driver).__name__)


def get_driver() -> "Pmp450iSnmpDriver | Pmp450iDriver":
    """Return the injected driver or raise `DriverError` if not set.

    The return type is the union of both driver classes so callers
    that only need the legacy `fetch_radio_metrics` API keep their
    `Pmp450iDriver` reference, while new callers can narrow it to
    `Pmp450iSnmpDriver` for the Protocol surface.
    """
    if _driver is None:
        raise DriverError(
            "driver is not initialised; call nora.drivers.registry.set_driver(...) "
            "from the boot sequence (nora.__main__:main)"
        )
    return _driver


__all__ = ["set_driver", "get_driver"]
