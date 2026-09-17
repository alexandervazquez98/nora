"""WU-1 — puresnmp wire-type decoding + async-generator drain (Issue #48).

Issue: https://github.com/alexandervazquez98/nora/issues/48

`puresnmp.PyWrapper` surfaces `OCTET STRING` as native Python `bytes`
and `TimeTicks` as `datetime.timedelta`. The driver previously rejected
these with `NetworkUnreachableError("unexpected scalar type bytes")`.

It also passed `walk()`'s `async_generator` straight to `asyncio.run()`,
which raises `ValueError: a coroutine was expected, got async_generator`.

These tests mock at the `puresnmp.PyWrapper` class attribute so we
exercise the client's normalization/drain code, not puresnmp itself.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, AsyncIterator

import pytest
from pydantic import SecretStr

from nora.drivers.exceptions import NetworkUnreachableError
from nora.drivers.inventory import Device
from nora.drivers.snmp_pmp450i.v2c import V2CClient
from nora.drivers.snmp_pmp450i.v3 import V3Client

# ---------------------------------------------------------------------------
# Device factories
# ---------------------------------------------------------------------------


def _v2c_device() -> Device:
    """Minimal v2c Device for unit-level client construction."""
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


def _v3_device() -> Device:
    """Minimal v3 Device for unit-level client construction."""
    return Device(
        device_id="ap-7400-v3",
        vendor="cambium",
        model="pmp450i",
        firmware="15.2.1",
        host="127.0.0.1",
        port=16161,
        snmp_version="v3",
        auth_password=SecretStr("authpass1234"),
        priv_password=SecretStr("privpass1234"),
    )


# ---------------------------------------------------------------------------
# Mock helpers — patch `PyWrapper.get` and `PyWrapper.walk`
# ---------------------------------------------------------------------------


def _patch_pywrapper_get(monkeypatch: pytest.MonkeyPatch, value: Any) -> None:
    """Replace `puresnmp.PyWrapper.get` with an async function returning `value`."""
    from puresnmp.api import pythonic as _py

    async def fake_get(self: Any, oid: str) -> Any:
        return value

    monkeypatch.setattr(_py.PyWrapper, "get", fake_get)


def _patch_pywrapper_walk(monkeypatch: pytest.MonkeyPatch, items: list[tuple[str, Any]]) -> None:
    """Replace `puresnmp.PyWrapper.walk` with an async generator yielding `items`."""
    from puresnmp.api import pythonic as _py

    async def fake_walk(
        self: Any, oid: str, errors: str = "strict"
    ) -> AsyncIterator[tuple[str, Any]]:
        for item in items:
            yield item

    monkeypatch.setattr(_py.PyWrapper, "walk", fake_walk)


# ===========================================================================
# V2CClient — get_oid wire-type decoding
# ===========================================================================


def test_v2c_get_oid_octet_string_bytes_returns_str(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OCTET STRING comes back as bytes; client decodes to str.

    Reproduces the `sysDescr` walk on a real Cambium PMP 450i:
    `PyWrapper.get('1.3.6.1.2.1.1.1.0')` returns `b'CANOPY 25.1 AP'`.
    """
    _patch_pywrapper_get(monkeypatch, b"CANOPY 25.1 AP")
    client = V2CClient(_v2c_device())
    assert client.get_oid("1.3.6.1.2.1.1.1.0") == "CANOPY 25.1 AP"


def test_v2c_get_oid_octet_string_bytes_strips_whitespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """bytes with trailing whitespace get cleaned by .strip().

    Real Cambium `apFirmwareVersion` comes back like
    `b'CANOPY 25.1 AP\\n'`. Stripping prevents spurious diffs in
    downstream reports.
    """
    _patch_pywrapper_get(monkeypatch, b"  Cambium PMP 450i  \n")
    client = V2CClient(_v2c_device())
    assert client.get_oid("1.3.6.1.4.1.161.19.3.3.1.1.0") == "Cambium PMP 450i"


def test_v2c_get_oid_binary_bytes_falls_back_to_hex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-UTF8 bytes fall back to .hex() so binary blobs remain observable.

    Some OctetString OIDs carry raw MACs or model serials. We never want
    to crash the operator over an unexpected byte sequence.
    """
    _patch_pywrapper_get(monkeypatch, b"\xde\xad\xbe\xef")
    client = V2CClient(_v2c_device())
    assert client.get_oid("1.3.6.1.2.1.1.1.0") == "deadbeef"


def test_v2c_get_oid_timedelta_returns_int_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TimeTicks comes back as datetime.timedelta; client returns int total-seconds.

    Reproduces the `sysUpTime.0` walk — puresnmp surfaces this as
    `datetime.timedelta`, not `int`.
    """
    delta = timedelta(days=1, hours=2, minutes=3, seconds=4)
    _patch_pywrapper_get(monkeypatch, delta)
    client = V2CClient(_v2c_device())
    result = client.get_oid("1.3.6.1.2.1.1.3.0")
    assert result == int(delta.total_seconds())
    assert isinstance(result, int)


def test_v2c_get_oid_int_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    """int values pass through unchanged (regression guard)."""
    _patch_pywrapper_get(monkeypatch, 54000000)
    client = V2CClient(_v2c_device())
    assert client.get_oid("1.3.6.1.4.1.161.19.3.1.1.1.0") == 54000000


def test_v2c_get_oid_str_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    """str values pass through unchanged (regression guard)."""
    _patch_pywrapper_get(monkeypatch, "256QAM")
    client = V2CClient(_v2c_device())
    assert client.get_oid("1.3.6.1.4.1.161.19.3.1.1.6.0") == "256QAM"


def test_v2c_get_oid_unknown_type_still_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown wire types (not bytes/timedelta/str/int) must keep failing closed.

    Belt-and-braces: we don't want a future odd type (e.g. `float`) to
    silently leak through.
    """
    _patch_pywrapper_get(monkeypatch, 3.14)  # float is not str/int/bytes/timedelta
    client = V2CClient(_v2c_device())
    with pytest.raises(NetworkUnreachableError) as exc_info:
        client.get_oid("1.3.6.1.2.1.1.1.0")
    assert "unexpected scalar type" in str(exc_info.value)
    assert "float" in str(exc_info.value)


# ===========================================================================
# V2CClient — walk async-generator drain
# ===========================================================================


def test_v2c_walk_drains_async_generator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PyWrapper.walk returns an async generator; client must drain it.

    Without the fix, `asyncio.run(async_generator)` raises
    `ValueError: a coroutine was expected, got async_generator`.
    """
    _patch_pywrapper_walk(
        monkeypatch,
        [
            ("1.3.6.1.4.1.161.19.3.1.7.1.1.1", 1),
            ("1.3.6.1.4.1.161.19.3.1.7.1.1.2", 2),
            ("1.3.6.1.4.1.161.19.3.1.7.1.1.3", 3),
        ],
    )
    client = V2CClient(_v2c_device())
    result = client.walk("1.3.6.1.4.1.161.19.3.1.7")
    assert result == [
        ("1.3.6.1.4.1.161.19.3.1.7.1.1.1", 1),
        ("1.3.6.1.4.1.161.19.3.1.7.1.1.2", 2),
        ("1.3.6.1.4.1.161.19.3.1.7.1.1.3", 3),
    ]


def test_v2c_walk_normalizes_bytes_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """walk values that come back as bytes get decoded through the same normalization."""
    _patch_pywrapper_walk(
        monkeypatch,
        [
            ("1.3.6.1.4.1.161.19.3.1.7.1.0", b"v15.2.1"),
            ("1.3.6.1.4.1.161.19.3.1.7.2.0", timedelta(seconds=3600)),
        ],
    )
    client = V2CClient(_v2c_device())
    result = client.walk("1.3.6.1.4.1.161.19.3.1.7")
    assert result == [
        ("1.3.6.1.4.1.161.19.3.1.7.1.0", "v15.2.1"),
        ("1.3.6.1.4.1.161.19.3.1.7.2.0", 3600),
    ]


# ===========================================================================
# V3Client — mirror of V2C wire-type tests
# ===========================================================================


def test_v3_get_oid_octet_string_bytes_returns_str(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pywrapper_get(monkeypatch, b"CANOPY 25.1 AP")
    client = V3Client(_v3_device())
    assert client.get_oid("1.3.6.1.2.1.1.1.0") == "CANOPY 25.1 AP"


def test_v3_get_oid_binary_bytes_falls_back_to_hex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pywrapper_get(monkeypatch, b"\xde\xad\xbe\xef")
    client = V3Client(_v3_device())
    assert client.get_oid("1.3.6.1.2.1.1.1.0") == "deadbeef"


def test_v3_get_oid_timedelta_returns_int_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pywrapper_get(monkeypatch, timedelta(seconds=93784))
    client = V3Client(_v3_device())
    assert client.get_oid("1.3.6.1.2.1.1.3.0") == 93784


def test_v3_get_oid_int_passthrough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pywrapper_get(monkeypatch, 54000000)
    client = V3Client(_v3_device())
    assert client.get_oid("1.3.6.1.4.1.161.19.3.1.1.1.0") == 54000000


def test_v3_get_oid_str_passthrough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pywrapper_get(monkeypatch, "256QAM")
    client = V3Client(_v3_device())
    assert client.get_oid("1.3.6.1.4.1.161.19.3.1.1.6.0") == "256QAM"


def test_v3_walk_drains_async_generator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pywrapper_walk(
        monkeypatch,
        [
            ("1.3.6.1.4.1.161.19.3.1.7.1.1.1", 1),
            ("1.3.6.1.4.1.161.19.3.1.7.1.1.2", 2),
        ],
    )
    client = V3Client(_v3_device())
    result = client.walk("1.3.6.1.4.1.161.19.3.1.7")
    assert result == [
        ("1.3.6.1.4.1.161.19.3.1.7.1.1.1", 1),
        ("1.3.6.1.4.1.161.19.3.1.7.1.1.2", 2),
    ]


def test_v3_walk_normalizes_bytes_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pywrapper_walk(
        monkeypatch,
        [
            ("1.3.6.1.4.1.161.19.3.1.7.1.0", b"v15.2.1"),
        ],
    )
    client = V3Client(_v3_device())
    result = client.walk("1.3.6.1.4.1.161.19.3.1.7")
    assert result == [("1.3.6.1.4.1.161.19.3.1.7.1.0", "v15.2.1")]


def test_v3_walk_normalizes_timedelta_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pywrapper_walk(
        monkeypatch,
        [
            ("1.3.6.1.2.1.1.3.0", timedelta(seconds=12345)),
        ],
    )
    client = V3Client(_v3_device())
    result = client.walk("1.3.6.1.2.1.1.3")
    assert result == [("1.3.6.1.2.1.1.3.0", 12345)]
