"""WU-2 — V3Client propagates `timeout` / `retries` to the underlying
`puresnmp.Client`.

Mirrors `test_v2c_client_timeout.py` for the v3 (auth+priv) client.
"""

from __future__ import annotations

from typing import Any, Callable

import pytest
from pydantic import SecretStr

from nora.drivers.exceptions import SnmpTimeoutError
from nora.drivers.inventory import Device
from nora.drivers.snmp_pmp450i.v3 import V3Client

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _make_v3_device() -> Device:
    """Minimal v3 `Device` (auth+priv) for unit-level client construction."""
    return Device(
        device_id="sm-7400-v3",
        vendor="cambium",
        model="pmp450i",
        firmware="15.2.1",
        host="127.0.0.1",
        port=16161,
        snmp_version="v3",
        auth_password=SecretStr("change-me-auth"),
        priv_password=SecretStr("change-me-priv"),
    )


@pytest.fixture
def v3_device() -> Device:
    return _make_v3_device()


def _spy_on_configure(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[dict[str, Any]], Callable[[Any], Any]]:
    """Monkeypatch `puresnmp.Client.configure` with a recording spy."""
    from puresnmp.api import raw as _raw

    original = _raw.Client.configure
    calls: list[dict[str, Any]] = []

    def spy(self: Any, **kwargs: Any) -> None:
        calls.append(dict(kwargs))
        return original(self, **kwargs)

    monkeypatch.setattr(_raw.Client, "configure", spy)
    return calls, original


def _inject_spy_sender(
    monkeypatch: pytest.MonkeyPatch,
) -> list[dict[str, Any]]:
    """Replace `_RawClient.__init__` so the closure captures our spy sender."""
    from puresnmp.api import raw as _raw
    from puresnmp.exc import Timeout as _PuresnmpTimeout

    calls: list[dict[str, Any]] = []

    async def spy_send(endpoint: Any, packet: bytes, **kwargs: Any) -> bytes:
        calls.append(dict(kwargs))
        raise _PuresnmpTimeout("captured by test_v3_client_timeout")

    original_init = _raw.Client.__init__

    def patched_init(self: Any, ip: Any, credentials: Any, port: int = 161, **kw: Any) -> None:
        return original_init(self, ip, credentials, port, sender=spy_send, **kw)

    monkeypatch.setattr(_raw.Client, "__init__", patched_init)
    return calls


# ---------------------------------------------------------------------------
# Scenario 1 — explicit kwargs reach _RawClient.configure()
# ---------------------------------------------------------------------------


def test_v3_init_explicit_kwargs_reach_configure(
    monkeypatch: pytest.MonkeyPatch, v3_device: Device
) -> None:
    """`V3Client(device, timeout=2.0, retries=1)` must call
    `_RawClient.configure(timeout=2.0, retries=1)`."""
    calls, _ = _spy_on_configure(monkeypatch)

    V3Client(v3_device, timeout=2.0, retries=1)

    assert calls == [{"timeout": 2.0, "retries": 1}], (
        f"expected a single configure(timeout=2.0, retries=1) call; got {calls!r}"
    )


def test_v3_init_explicit_kwargs_propagate_to_runtime_config(
    monkeypatch: pytest.MonkeyPatch, v3_device: Device
) -> None:
    """After construction, `client._client.client.config.timeout` /
    `.retries` must reflect the operator's values (not puresnmp
    defaults 6 / 10)."""
    _spy_on_configure(monkeypatch)

    client = V3Client(v3_device, timeout=2.0, retries=1)

    assert client._client.client.config.timeout == 2.0
    assert client._client.client.config.retries == 1


# ---------------------------------------------------------------------------
# Scenario 2 — default kwargs (5.0 / 1) reach _RawClient.configure()
# ---------------------------------------------------------------------------


def test_v3_init_default_kwargs_reach_configure(
    monkeypatch: pytest.MonkeyPatch, v3_device: Device
) -> None:
    """`V3Client(device)` (no kwargs) must call
    `_RawClient.configure(timeout=5.0, retries=1)` — the public
    defaults."""
    calls, _ = _spy_on_configure(monkeypatch)

    V3Client(v3_device)

    assert calls == [{"timeout": 5.0, "retries": 1}], (
        f"expected a single configure(timeout=5.0, retries=1) call; got {calls!r}"
    )


def test_v3_init_default_kwargs_propagate_to_runtime_config(
    v3_device: Device,
) -> None:
    """After construction with no kwargs, `client._client.client.config`
    must hold `timeout=5.0` and `retries=1`."""
    client = V3Client(v3_device)

    assert client._client.client.config.timeout == 5.0
    assert client._client.client.config.retries == 1


# ---------------------------------------------------------------------------
# Scenario 3 — configured timeout reaches the UDP sender at request time
# ---------------------------------------------------------------------------


def test_v3_configured_timeout_reaches_sender(
    monkeypatch: pytest.MonkeyPatch, v3_device: Device
) -> None:
    """A `get_oid` call must hand the operator's timeout/retries to
    the UDP sender closure (the actual wire-level timeout)."""
    sender_calls = _inject_spy_sender(monkeypatch)

    client = V3Client(v3_device, timeout=2.0, retries=1)

    with pytest.raises(SnmpTimeoutError):
        client.get_oid("1.3.6.1.2.1.1.1.0")

    assert len(sender_calls) == 1, (
        f"expected exactly one sender invocation; got {len(sender_calls)}"
    )
    assert sender_calls[0]["timeout"] == 2.0
    assert sender_calls[0]["retries"] == 1


def test_v3_default_timeout_reaches_sender(
    monkeypatch: pytest.MonkeyPatch, v3_device: Device
) -> None:
    """A `get_oid` call with default kwargs must hand 5.0 / 1 to the
    UDP sender — not the puresnmp defaults (6 / 10)."""
    sender_calls = _inject_spy_sender(monkeypatch)

    client = V3Client(v3_device)

    with pytest.raises(SnmpTimeoutError):
        client.get_oid("1.3.6.1.2.1.1.1.0")

    assert sender_calls[0]["timeout"] == 5.0
    assert sender_calls[0]["retries"] == 1


# ---------------------------------------------------------------------------
# Belt-and-braces — make sure we did not regress the no-spy path
# ---------------------------------------------------------------------------


def test_v3_init_does_not_use_puresnmp_defaults(
    v3_device: Device,
) -> None:
    """Without any kwargs, `config.timeout` must NOT be the puresnmp
    default of 6 — it must be the public default of 5.0."""
    client = V3Client(v3_device)

    assert client._client.client.config.timeout != 6
    assert client._client.client.config.retries != 10
