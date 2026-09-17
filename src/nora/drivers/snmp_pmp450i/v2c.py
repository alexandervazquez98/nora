"""V2CClient — synchronous SNMPv2c client over `puresnmp.PyWrapper`.

`puresnmp.Client` is async at the wire level; `PyWrapper` provides
typed conversions but is still async. We wrap each call in `asyncio.run`
to expose a synchronous read-only surface that the driver can call
without pytest-asyncio or an event loop.

Errors are mapped to typed driver exceptions.
"""

from __future__ import annotations

import asyncio
import inspect
import ipaddress
from datetime import timedelta
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
    """SNMPv2c client wrapping `puresnmp.PyWrapper` (synchronous facade).

    The ``timeout`` (seconds) and ``retries`` keyword arguments
    propagate to the underlying ``puresnmp.Client`` via its
    documented ``configure(**kwargs)`` method, so they apply to
    every wire-level UDP send. Without this call, the client would
    silently use puresnmp's defaults (``timeout=6``, ``retries=10``).
    """

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
        # `puresnmp.Client` takes positional args; `timeout`/`retries`
        # are NOT constructor kwargs (verified against puresnmp's API),
        # so we apply them via the documented `configure()` method
        # immediately after construction. `PyWrapper` exposes the raw
        # client as `.client`; `configure(**kwargs)` permanently
        # replaces the underlying `ClientConfig`.
        self._client: Any = PyWrapper(
            _RawClient(
                device.host,
                V2C(community_value),
                device.port,
            )
        )
        self._client.client.configure(
            timeout=self._timeout,
            retries=self._retries,
        )

    # ------------------------------------------------------------------
    # public read-only API
    # ------------------------------------------------------------------

    def get_oid(self, oid: str) -> str | int:
        """SNMP GET; returns native Python scalar via `asyncio.run`.

        `puresnmp.PyWrapper.get` surfaces `OCTET STRING` as native
        Python `bytes` and `TimeTicks` as `datetime.timedelta`. We
        normalize both to the `str | int` Protocol contract here; the
        underlying wire types stay visible only inside this method.
        """
        result = self._call_async("get", oid)
        return self._normalize_scalar(result)

    def walk(self, base_oid: str) -> list[tuple[str, str | int]]:
        """SNMP WALK under `base_oid`. Read-only by definition.

        `puresnmp.PyWrapper.walk` is an `async def` generator function
        that returns an `AsyncGenerator[PyVarBind, None]`, NOT a
        coroutine. `_call_async` drains it; we then normalize each
        value through the same `_normalize_scalar` helper so the
        Protocol contract `list[tuple[str, str | int]]` holds for
        every wire type.
        """
        result = self._call_async("walk", base_oid)
        return [(str(o), self._normalize_scalar(value)) for o, value in result]

    def close(self) -> None:
        """No-op — `puresnmp.Client` is stateless."""
        return None

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_scalar(value: Any) -> str | int:
        """Normalize puresnmp wire types to the `str | int` Protocol contract.

        - `bytes` (UTF-8, e.g. OCTET STRING `sysDescr`) -> decoded + stripped `str`
        - `bytes` (non-UTF-8, e.g. MAC or raw serial)    -> `.hex()` `str` fallback
        - `datetime.timedelta` (TimeTicks `sysUpTime`)   -> `int(total_seconds())`
        - `ipaddress.IPv4Address` / `IPv6Address`        -> `str(value)` (e.g. '192.0.2.1')
        - `str` / `int` (already-native)                 -> passthrough
        - anything else                                  -> raises `NetworkUnreachableError`
        """
        if isinstance(value, bytes):
            try:
                return value.decode("utf-8").strip()
            except UnicodeDecodeError:
                return value.hex()
        if isinstance(value, timedelta):
            return int(value.total_seconds())
        if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
            return str(value)
        if isinstance(value, (str, int)):
            return value
        raise NetworkUnreachableError(f"unexpected scalar type {type(value).__name__}")

    def _call_async(self, method_name: str, *args: Any) -> Any:
        # `puresnmp.PyWrapper` returns a coroutine for `get`/`getnext`/
        # `multiget`/`bulkget`/`table` but an `async_generator` for
        # `walk`/`multiwalk`/`bulkwalk`. `asyncio.run` rejects async
        # generators with `ValueError`, so we dispatch by introspection
        # and drain the generator inside a short-lived event loop.
        coro = getattr(self._client, method_name)(*args)
        run_target: Any
        if inspect.iscoroutine(coro):
            run_target = coro
        elif inspect.isasyncgen(coro):

            async def _drain() -> list[Any]:
                return [item async for item in coro]

            run_target = _drain()
        else:
            raise NetworkUnreachableError(
                f"{self._device.host}:{self._device.port}: "
                f"unexpected return type from {method_name}: "
                f"{type(coro).__name__}"
            )
        try:
            result: Any = asyncio.run(run_target)
            return result
        except (TimeoutError, asyncio.TimeoutError) as exc:
            # `socket.timeout` is a subclass of `TimeoutError` on
            # Python 3.10+, so this catch covers raw socket timeouts
            # without us importing `socket`.
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
