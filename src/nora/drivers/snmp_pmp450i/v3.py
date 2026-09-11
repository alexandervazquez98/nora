"""V3Client — synchronous SNMPv3 client (auth + priv) over `puresnmp`.

Mirrors `V2CClient` but uses `V3(user, Auth(key, method), Priv(key, method))`.
`puresnmp-crypto` MUST be installed (it's a runtime dep in
`pyproject.toml`) — without it, `puresnmp.Auth` / `puresnmp.Priv`
construction still works because they are plain namedtuples, but the
HMAC / encryption routines aren't available.
"""

from __future__ import annotations

import asyncio
import socket
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

from .client import SnmpClient

# Default auth / priv protocols. SHA + AES-128 are the Cambium PMP 450i
# defaults; operators needing a different pair set explicit values via
# Device extensions in Phase 3 (out of scope here).
_DEFAULT_AUTH_PROTOCOL: str = "sha"
_DEFAULT_PRIV_PROTOCOL: str = "aes"
_DEFAULT_USER: str = "nora"


class V3Client:
    """SNMPv3 client (auth + priv) wrapping `puresnmp.PyWrapper`."""

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

        self._client: Any = PyWrapper(
            _RawClient(
                device.host,
                V3(user, auth, priv),
                device.port,
            )
        )

    # ------------------------------------------------------------------
    # public read-only API
    # ------------------------------------------------------------------

    def get_oid(self, oid: str) -> str | int:
        """SNMPv3 GET; returns native Python scalar."""
        result = self._call_async("get", oid)
        if not isinstance(result, (str, int)):
            raise NetworkUnreachableError(
                f"{self._device.host}:{self._device.port}: "
                f"unexpected scalar type {type(result).__name__}"
            )
        return result

    def walk(self, base_oid: str) -> list[tuple[str, str | int]]:
        """SNMPv3 WALK under `base_oid`. Read-only by definition."""
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


def make_v3_client(device: Device) -> SnmpClient:
    """Factory used by the driver to construct a v3 client."""
    return V3Client(device)


__all__ = ["V3Client", "make_v3_client"]
