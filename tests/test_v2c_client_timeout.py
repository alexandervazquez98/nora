"""WU-2 — V2CClient propagates `timeout` / `retries` to the underlying
`puresnmp.Client`.

PR #44 sandbox observation #2 found that the constructor accepted
`timeout`/`retries` kwargs but never propagated them to the wire, so
`register_device(validate=True)` against an unreachable IP took
~60s before failing (puresnmp defaults: timeout=6s, retries=10).

Three scenarios:
1. Explicit `timeout=2.0, retries=1` reaches `_RawClient.configure(...)`.
2. The public defaults (5.0 / 1) reach `_RawClient.configure(...)`.
3. The configured timeout reaches the UDP sender at request time.

The tests deliberately avoid mutating `client.config` directly
(spec forbids bypassing `puresnmp`'s `configure()`) — they spy on
`configure()` and on the sender closure instead.
"""

from __future__ import annotations

from typing import Any, Callable

import pytest
from pydantic import SecretStr

from nora.drivers.exceptions import SnmpTimeoutError
from nora.drivers.inventory import Device
from nora.drivers.snmp_pmp450i.v2c import V2CClient

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _make_v2c_device() -> Device:
    """Minimal v2c `Device` for unit-level client construction."""
    return Device(
        device_id="ap-7400-v2c",
        vendor="cambium",
        model="pmp450i",
        firmware="15.2.1",
        host="127.0.0.1",
        port=16161,
        snmp_version="v2c",
        community=SecretStr("public"),
    )


@pytest.fixture
def v2c_device() -> Device:
    return _make_v2c_device()


def _spy_on_configure(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[dict[str, Any]], Callable[[Any], Any]]:
    """Monkeypatch `puresnmp.Client.configure` with a recording spy.

    The spy delegates to the original method so the runtime
    `ClientConfig` actually gets replaced — we are testing the
    WIRING, not bypassing the API.

    Returns `(calls, restore)`. `calls` is the list of kwargs dicts
    each `configure(...)` invocation saw.
    """
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
    """Replace `_RawClient.__init__` so the closure captures our spy sender.

    `Client.__init__` builds a closure `handler` that does
    `await sender(endpoint, data, timeout=self.config.timeout, ...)`.
    The default `sender` is bound at function-definition time, so we
    wrap `__init__` to pass our spy as an explicit `sender=...` arg.
    The spy raises `puresnmp.exc.Timeout` to short-circuit the wire
    after recording the kwargs — the existing `_call_async` maps it
    to `SnmpTimeoutError`.
    """
    from puresnmp.api import raw as _raw
    from puresnmp.exc import Timeout as _PuresnmpTimeout

    calls: list[dict[str, Any]] = []

    async def spy_send(endpoint: Any, packet: bytes, **kwargs: Any) -> bytes:
        calls.append(dict(kwargs))
        raise _PuresnmpTimeout("captured by test_v2c_client_timeout")

    original_init = _raw.Client.__init__

    def patched_init(self: Any, ip: Any, credentials: Any, port: int = 161, **kw: Any) -> None:
        # Forward the spy as the explicit `sender` kwarg so the inner
        # handler closure captures it.
        return original_init(self, ip, credentials, port, sender=spy_send, **kw)

    monkeypatch.setattr(_raw.Client, "__init__", patched_init)
    return calls


# ---------------------------------------------------------------------------
# Scenario 1 — explicit kwargs reach _RawClient.configure()
# ---------------------------------------------------------------------------


def test_v2c_init_explicit_kwargs_reach_configure(
    monkeypatch: pytest.MonkeyPatch, v2c_device: Device
) -> None:
    """`V2CClient(device, timeout=2.0, retries=1)` must call
    `_RawClient.configure(timeout=2.0, retries=1)`."""
    calls, _ = _spy_on_configure(monkeypatch)

    V2CClient(v2c_device, timeout=2.0, retries=1)

    assert calls == [{"timeout": 2.0, "retries": 1}], (
        f"expected a single configure(timeout=2.0, retries=1) call; got {calls!r}"
    )


def test_v2c_init_explicit_kwargs_propagate_to_runtime_config(
    monkeypatch: pytest.MonkeyPatch, v2c_device: Device
) -> None:
    """After construction, `client._client.client.config.timeout` /
    `.retries` must reflect the operator's values (not puresnmp
    defaults 6 / 10)."""
    _spy_on_configure(monkeypatch)

    client = V2CClient(v2c_device, timeout=2.0, retries=1)

    assert client._client.client.config.timeout == 2.0
    assert client._client.client.config.retries == 1


# ---------------------------------------------------------------------------
# Scenario 2 — default kwargs (5.0 / 1) reach _RawClient.configure()
# ---------------------------------------------------------------------------


def test_v2c_init_default_kwargs_reach_configure(
    monkeypatch: pytest.MonkeyPatch, v2c_device: Device
) -> None:
    """`V2CClient(device)` (no kwargs) must call
    `_RawClient.configure(timeout=5.0, retries=1)` — the public
    defaults."""
    calls, _ = _spy_on_configure(monkeypatch)

    V2CClient(v2c_device)

    assert calls == [{"timeout": 5.0, "retries": 1}], (
        f"expected a single configure(timeout=5.0, retries=1) call; got {calls!r}"
    )


def test_v2c_init_default_kwargs_propagate_to_runtime_config(
    v2c_device: Device,
) -> None:
    """After construction with no kwargs, `client._client.client.config`
    must hold `timeout=5.0` and `retries=1`."""
    client = V2CClient(v2c_device)

    assert client._client.client.config.timeout == 5.0
    assert client._client.client.config.retries == 1


# ---------------------------------------------------------------------------
# Scenario 3 — configured timeout reaches the UDP sender at request time
# ---------------------------------------------------------------------------


def test_v2c_configured_timeout_reaches_sender(
    monkeypatch: pytest.MonkeyPatch, v2c_device: Device
) -> None:
    """A `get_oid` call must hand the operator's timeout/retries to
    the UDP sender closure (the actual wire-level timeout)."""
    sender_calls = _inject_spy_sender(monkeypatch)

    client = V2CClient(v2c_device, timeout=2.0, retries=1)

    # Spy raises puresnmp.exc.Timeout; `_call_async` maps it to SnmpTimeoutError.
    with pytest.raises(SnmpTimeoutError):
        client.get_oid("1.3.6.1.2.1.1.1.0")

    assert len(sender_calls) == 1, (
        f"expected exactly one sender invocation; got {len(sender_calls)}"
    )
    assert sender_calls[0]["timeout"] == 2.0
    assert sender_calls[0]["retries"] == 1


def test_v2c_default_timeout_reaches_sender(
    monkeypatch: pytest.MonkeyPatch, v2c_device: Device
) -> None:
    """A `get_oid` call with default kwargs must hand 5.0 / 1 to the
    UDP sender — not the puresnmp defaults (6 / 10)."""
    sender_calls = _inject_spy_sender(monkeypatch)

    client = V2CClient(v2c_device)

    with pytest.raises(SnmpTimeoutError):
        client.get_oid("1.3.6.1.2.1.1.1.0")

    assert sender_calls[0]["timeout"] == 5.0
    assert sender_calls[0]["retries"] == 1


# ---------------------------------------------------------------------------
# Belt-and-braces — make sure we did not regress the no-spy path
# ---------------------------------------------------------------------------


def test_v2c_init_does_not_use_puresnmp_defaults(
    v2c_device: Device,
) -> None:
    """Without any kwargs, `config.timeout` must NOT be the puresnmp
    default of 6 — it must be the public default of 5.0."""
    client = V2CClient(v2c_device)

    # Belt-and-braces: assert we did NOT inherit puresnmp's defaults.
    assert client._client.client.config.timeout != 6
    assert client._client.client.config.retries != 10
