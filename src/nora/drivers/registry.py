"""Driver registry — module-level singleton for the PMP 450i driver.

`__main__.py` calls `set_driver(driver)` after the boot sequence
loads the inventory + verifies the catalog. The MCP tool calls
`get_driver()` to fetch the singleton; tests inject a fake via
`set_driver(fake)`.

A `None` singleton surfaces as `DriverError("driver not initialised")`
when `get_driver()` is called before `set_driver(...)` — this matches
the boot-fatal contract from `__main__.py`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from nora.drivers.exceptions import DriverError

if TYPE_CHECKING:
    from nora.drivers.snmp_pmp450i import Pmp450iDriver

logger = logging.getLogger("nora.drivers.registry")


_driver: "Pmp450iDriver | None" = None


def set_driver(driver: "Pmp450iDriver | None") -> None:
    """Inject (or clear) the module-level driver singleton.

    Used by `__main__.py` at boot and by tests via `monkeypatch`.
    """
    global _driver
    _driver = driver
    if driver is None:
        logger.debug("driver registry cleared")
    else:
        logger.debug("driver registry injected: %s", type(driver).__name__)


def get_driver() -> "Pmp450iDriver":
    """Return the injected driver or raise `DriverError` if not set."""
    if _driver is None:
        raise DriverError(
            "driver is not initialised; call nora.drivers.registry.set_driver(...) "
            "from the boot sequence (nora.__main__:main)"
        )
    return _driver


__all__ = ["set_driver", "get_driver"]
