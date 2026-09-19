"""V3Client — synchronous SNMPv3 client (auth + priv) over `puresnmp`.

Mirrors `V2CClient` but uses `V3(user, Auth(key, method), Priv(key, method))`.
`puresnmp-crypto` MUST be installed (it's a runtime dep in
`pyproject.toml`) — without it, `puresnmp.Auth` / `puresnmp.Priv`
construction still works because they are plain namedtuples, but the
HMAC / encryption routines aren't available.

Driver-R2 carve-out (issue #62, 2026-09-19): the read-only
``V3Client`` stays read-only. The write capability lives on
``WritableV3Client`` (below) which wraps a ``V3Client`` and forwards
``get_oid`` / ``walk`` / ``close`` to it, exposing ``set`` only via
the ``puresnmp.PyWrapper.set`` async coroutine. The carve-out is
narrowly scoped to this file (``v3.py``) — see
``tests/test_driver_snmp450i_readonly.py::_WRITABLE_SEAM_FILES``.
"""

from __future__ import annotations

import asyncio
import inspect
import ipaddress
from datetime import timedelta
from typing import Any

from puresnmp import Auth as _Auth
from puresnmp import Client as _RawClient
from puresnmp import Priv as _Priv
from puresnmp import PyWrapper
from puresnmp.credentials import V3

from nora.drivers.exceptions import (
    NetworkUnreachableError,
    SnmpTimeoutError,
)
from nora.drivers.inventory import Device

from .client import SnmpClient, WritableSnmpClient

# Default auth / priv protocols. SHA + AES-128 are the Cambium PMP 450i
# defaults; operators needing a different pair set explicit values via
# Device extensions in Phase 3 (out of scope here).
_DEFAULT_AUTH_PROTOCOL: str = "sha"
_DEFAULT_PRIV_PROTOCOL: str = "aes"
_DEFAULT_USER: str = "nora"


class V3Client:
    """SNMPv3 client (auth + priv) wrapping `puresnmp.PyWrapper`.

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
        auth_protocol: str = _DEFAULT_AUTH_PROTOCOL,
        priv_protocol: str = _DEFAULT_PRIV_PROTOCOL,
        timeout: float = 5.0,
        retries: int = 1,
        user: str = _DEFAULT_USER,
    ) -> None:
        if device.snmp_version != "v3":
            raise ValueError("V3Client requires snmp_version='v3'")
        if device.auth_password is None or device.priv_password is None:
            raise ValueError("V3Client requires both auth_password and priv_password")

        self._device = device
        self._timeout = timeout
        self._retries = retries

        auth_value = device.auth_password.get_secret_value()
        priv_value = device.priv_password.get_secret_value()
        auth = _Auth(auth_value.encode("utf-8"), auth_protocol.lower())
        priv = _Priv(priv_value.encode("utf-8"), priv_protocol.lower())

        # `puresnmp.Client` takes positional args; `timeout`/`retries`
        # are NOT constructor kwargs (verified against puresnmp's API),
        # so we apply them via the documented `configure()` method
        # immediately after construction. `PyWrapper` exposes the raw
        # client as `.client`; `configure(**kwargs)` permanently
        # replaces the underlying `ClientConfig`.
        self._client: Any = PyWrapper(
            _RawClient(
                device.host,
                V3(user, auth, priv),
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
        """SNMPv3 GET; returns native Python scalar.

        `puresnmp.PyWrapper.get` surfaces `OCTET STRING` as native
        Python `bytes` and `TimeTicks` as `datetime.timedelta`. We
        normalize both to the `str | int` Protocol contract here; the
        underlying wire types stay visible only inside this method.
        """
        result = self._call_async("get", oid)
        return self._normalize_scalar(result)

    def walk(self, base_oid: str) -> list[tuple[str, str | int]]:
        """SNMPv3 WALK under `base_oid`. Read-only by definition.

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


def make_v3_client(device: Device) -> SnmpClient:
    """Factory used by the driver to construct a v3 client."""
    return V3Client(device)


# ---------------------------------------------------------------------------
# Writable seam — Driver-R2 carve-out (issue #62, 2026-09-19)
# ---------------------------------------------------------------------------


class WritableV3Client:
    """Write-capable adapter wrapping a read-only :class:`V3Client`.

    Mirror of :class:`WritableV2CClient` for SNMPv3 — same thin
    forward-the-read-only-surface-and-add-`set` pattern. The Cambium
    WHISP-BOX-MIBV2-MIB sweep protocol requires SET frames regardless
    of credential family; both adapters expose the same Protocol
    surface (``get_oid`` / ``walk`` / ``close`` / ``set``) so the
    driver can pick one at wire-up time and not branch on v2c-vs-v3
    inside the sweep loop.
    """

    def __init__(
        self,
        client: V3Client | None = None,
        *,
        device: Device | None = None,
        auth_protocol: str = _DEFAULT_AUTH_PROTOCOL,
        priv_protocol: str = _DEFAULT_PRIV_PROTOCOL,
        timeout: float = 5.0,
        retries: int = 1,
        user: str = _DEFAULT_USER,
    ) -> None:
        if client is not None:
            if device is not None:
                raise ValueError("WritableV3Client: pass either `client` or `device`, not both")
            self._inner = client
        elif device is not None:
            self._inner = V3Client(
                device,
                auth_protocol=auth_protocol,
                priv_protocol=priv_protocol,
                timeout=timeout,
                retries=retries,
                user=user,
            )
        else:
            raise ValueError("WritableV3Client requires either an existing V3Client or a Device")

    # ------------------------------------------------------------------
    # Forwarded read-only surface
    # ------------------------------------------------------------------

    def get_oid(self, oid: str) -> str | int:
        return self._inner.get_oid(oid)

    def walk(self, base_oid: str) -> list[tuple[str, str | int]]:
        return self._inner.walk(base_oid)

    def close(self) -> None:
        self._inner.close()

    # ------------------------------------------------------------------
    # Writable seam — single `set` verb
    # ------------------------------------------------------------------

    def set(self, oid: str, value: str | int) -> None:
        """Emit one SNMPv3 SET frame against `oid` with `value`.

        Delegates to ``puresnmp.PyWrapper.set`` via the same async-run
        plumbing as ``V3Client._call_async``. Auth + priv credentials
        are inherited from the wrapped ``V3Client``; wire failures
        surface as typed driver exceptions via the existing exception
        mapping (``SnmpTimeoutError`` / ``NetworkUnreachableError``).
        """
        result = self._inner._call_async("set", oid, value)
        del result


def make_writable_v3_client(device: Device) -> WritableSnmpClient:
    """Factory used by the driver to construct a write-capable v3 client."""
    return WritableV3Client(device=device)


__all__ = ["V3Client", "WritableV3Client", "make_v3_client", "make_writable_v3_client"]
