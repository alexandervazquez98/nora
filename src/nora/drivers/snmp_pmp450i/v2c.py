"""V2CClient — synchronous SNMPv2c client over `puresnmp.PyWrapper`.

`puresnmp.Client` is async at the wire level; `PyWrapper` provides
typed conversions but is still async. We wrap each call in `asyncio.run`
to expose a synchronous read-only surface that the driver can call
without pytest-asyncio or an event loop.

Errors are mapped to typed driver exceptions.
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any

from puresnmp import Client as _RawClient
from puresnmp import PyWrapper
from puresnmp.credentials import V2C

from nora.drivers.exceptions import (
    NetworkUnreachableError,
    SnmpTimeoutError,
)
from nora.drivers.inventory import Device

from .client import SnmpClient


class V2CClient:
    """SNMPv2c client wrapping `puresnmp.PyWrapper` (synchronous facade)."""

    def __init__(
        self,
        device: Device,
        *,
        timeout: float = 5.0,
        retries: int = 1,
    ) -> None:
        if device.snmp_version != "v2c":
            raise ValueError("V2CClient requires snmp_version='v2c'")
        if device.community is None:
            raise ValueError("V2CClient requires a non-empty community string")
        self._device = device
        self._timeout = timeout
        self._retries = retries
        community_value = device.community.get_secret_value()
        # `puresnmp.Client` takes positional args; `timeout` is honoured
        # by the underlying asyncio.wait_for in `_call_async`.
        self._client: Any = PyWrapper(
            _RawClient(
                device.host,
                V2C(community_value),
                device.port,
            )
        )

    # ------------------------------------------------------------------
    # public read-only API
    # ------------------------------------------------------------------

    def get_oid(self, oid: str) -> str | int:
        """SNMP GET; returns native Python scalar via `asyncio.run`."""
        result = self._call_async("get", oid)
        if not isinstance(result, (str, int)):
            raise NetworkUnreachableError(
                f"{self._device.host}:{self._device.port}: "
                f"unexpected scalar type {type(result).__name__}"
            )
        return result

    def walk(self, base_oid: str) -> list[tuple[str, str | int]]:
        """SNMP WALK under `base_oid`. Read-only by definition."""
        result = self._call_async("walk", base_oid)
        return [(str(o), value) for o, value in result]

    def close(self) -> None:
        """No-op — `puresnmp.Client` is stateless."""
        return None

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _call_async(self, method_name: str, *args: Any) -> Any:
        coro = getattr(self._client, method_name)(*args)
        try:
            result: Any = asyncio.run(coro)
            return result
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise SnmpTimeoutError(str(args[0])) from exc
        except socket.timeout as exc:
            raise SnmpTimeoutError(str(args[0])) from exc
        except OSError as exc:
            raise NetworkUnreachableError(f"{self._device.host}:{self._device.port}") from exc
        except Exception as exc:
            from puresnmp.exc import SnmpError
            from puresnmp.exc import Timeout as _PuresnmpTimeout

            if isinstance(exc, _PuresnmpTimeout):
                raise SnmpTimeoutError(str(args[0])) from exc
            if isinstance(exc, SnmpError):
                raise NetworkUnreachableError(
                    f"{self._device.host}:{self._device.port}: {exc}"
                ) from exc
            raise NetworkUnreachableError(
                f"{self._device.host}:{self._device.port}: {exc}"
            ) from exc


def make_v2c_client(device: Device) -> SnmpClient:
    """Factory used by the driver to construct a v2c client."""
    return V2CClient(device)


__all__ = ["V2CClient", "make_v2c_client"]
