"""Tests for the unprivileged ICMP pinger (WU-1.1, WU-1.7).

These tests do not open real sockets. They inject a :class:`_FakeSocket`
via :func:`monkeypatch.setattr` on ``socket.socket`` so the engine sees a
controllable double per test.

Each async test is wrapped in :func:`asyncio.run` per the repo-wide ban
on ``pytest-asyncio`` (``openspec/changes/archive/.../phase2-pmp450i-driver/
design.md:233``).
"""

from __future__ import annotations

import asyncio
import socket
import struct
from typing import Any

import pytest

from nora.probes.exceptions import (
    IcmpTimeoutError,
    IcmpUnreachableError,
)
from nora.probes.icmp import (
    IcmpSample,
    UnprivilegedIcmpPinger,
    _compute_icmp_checksum,
)

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeSocket:
    """Minimal double for the engine's :func:`socket.socket` return value.

    Records ``sendto`` calls, returns pre-canned ``recv`` buffers, and
    pretends to be non-blocking so :func:`asyncio.loop.sock_recv` falls
    into its happy path (immediate result, no selector registration).
    When constructed with ``block_recv=True`` the ``recv`` call raises
    :class:`BlockingIOError` forever, simulating a peer that never
    replies so :func:`asyncio.wait_for` times out naturally.

    The fake owns a real :class:`socket.socket` so :meth:`fileno` returns
    a valid OS file descriptor; this lets the asyncio selector register
    the FD without raising :class:`OSError` during cancellation cleanup.
    """

    def __init__(
        self,
        recv_buffers: list[bytes] | None = None,
        block_recv: bool = False,
    ) -> None:
        self.sendto_calls: list[tuple[bytes, tuple[str, int]]] = []
        self._recv_buffers: list[bytes] = list(recv_buffers or [])
        self._block_recv = block_recv
        self.closed = False
        self.setblocking_called = False
        self.setblocking_value: bool | None = None
        # Real socket for a valid fileno; we never read/write through it.
        self._real_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def sendto(self, data: bytes, address: tuple[str, int]) -> int:
        self.sendto_calls.append((data, address))
        return len(data)

    def recv(self, bufsize: int) -> bytes:  # noqa: ARG002 — engine controls the bufsize
        if self._block_recv:
            raise BlockingIOError("fake socket blocks forever")
        if not self._recv_buffers:
            raise RuntimeError("fake socket recv underrun")
        return self._recv_buffers.pop(0)

    def setblocking(self, flag: bool) -> None:
        self.setblocking_called = True
        self.setblocking_value = flag

    def close(self) -> None:
        self.closed = True
        try:
            self._real_socket.close()
        except OSError:
            pass

    def fileno(self) -> int:
        return self._real_socket.fileno()


def _install_fake_socket(
    monkeypatch: pytest.MonkeyPatch,
    fake: _FakeSocket,
) -> None:
    """Patch :data:`socket.socket` so the engine builds our ``_FakeSocket``.

    Accepts any positional/keyword arguments because the asyncio event
    loop calls ``socket.socket(...)`` internally with a 4-arg form
    (``socket(family, type, proto, fileno)``) when it builds its self-pipe.
    We only return our fake for the 3-arg form the engine uses; the
    asyncio internals fall through to the real ``socket.socket``.
    """
    real_socket_factory = socket.socket  # capture before patching to break recursion

    def fake_factory(*_args: Any, **_kwargs: Any) -> Any:
        if len(_args) >= 4:
            return real_socket_factory(*_args, **_kwargs)
        return fake

    monkeypatch.setattr(socket, "socket", fake_factory)


def _build_icmp_packet(
    *,
    type_: int = 0,
    code: int = 0,
    identifier: int = 0,
    sequence: int = 0,
    payload: bytes = b"",
) -> bytes:
    """Build a valid ICMP packet with a correct checksum.

    Used as a canned ``recv`` payload for the engine's ``_send_and_recv``.
    """
    header = struct.pack(
        "!BBHHH",
        type_ & 0xFF,
        code & 0xFF,
        0,
        identifier & 0xFFFF,
        sequence & 0xFFFF,
    )
    data = header + payload
    checksum = _compute_icmp_checksum(data)
    header = struct.pack(
        "!BBHHH",
        type_ & 0xFF,
        code & 0xFF,
        checksum,
        identifier & 0xFFFF,
        sequence & 0xFFFF,
    )
    return header + payload


# ---------------------------------------------------------------------------
# _compute_icmp_checksum — pure unit test
# ---------------------------------------------------------------------------


def test_checksum_is_valid_rfc1071() -> None:
    """RFC 1071 ones-complement Internet checksum matches the known vector.

    Test vector (echo request, id=1, seq=0, no payload, checksum placeholder=0)::

        Input bytes: b"\\x08\\x00\\x00\\x00\\x00\\x01\\x00\\x00"
        Expected:    0xf7fe

    Derivation::

        big-endian words: 0x0800, 0x0000, 0x0001, 0x0000
        sum:              0x0801
        ones-complement:  0xf7fe
    """
    assert _compute_icmp_checksum(b"\x08\x00\x00\x00\x00\x01\x00\x00") == 0xF7FE


# ---------------------------------------------------------------------------
# UnprivilegedIcmpPinger.ping — behavioral coverage
# ---------------------------------------------------------------------------


def test_ping_returns_received_sample_when_socket_receives_echo_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fake socket returns a valid echo reply → engine returns ``received=True``."""
    fake = _FakeSocket(
        recv_buffers=[
            _build_icmp_packet(type_=0, code=0, identifier=1234, sequence=0, payload=b"hi"),
        ],
    )
    _install_fake_socket(monkeypatch, fake)

    async def _run() -> IcmpSample:
        pinger = UnprivilegedIcmpPinger()
        try:
            return await pinger.ping("192.0.2.10", payload_size=2, timeout=1.0)
        finally:
            await pinger.close()

    sample = asyncio.run(_run())

    assert isinstance(sample, IcmpSample)
    assert sample.target == "192.0.2.10"
    assert sample.received is True
    assert sample.error is None
    assert sample.rtt_ms is not None
    assert sample.rtt_ms >= 0.0
    assert sample.timestamp_unix > 0.0
    # Fake captured the outgoing ICMP echo request.
    assert len(fake.sendto_calls) == 1
    sent_packet, sent_address = fake.sendto_calls[0]
    assert sent_address == ("192.0.2.10", 0)
    # First byte of an echo request is the ICMP type (8).
    assert sent_packet[0] == 8
    # Header is 8 bytes + 2 bytes payload.
    assert len(sent_packet) == 10
    assert fake.setblocking_called is True
    assert fake.setblocking_value is False


def test_ping_raises_icmp_timeout_error_on_asyncio_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``asyncio.wait_for`` raises ``TimeoutError`` → ``IcmpTimeoutError``.

    We drive the timeout naturally by giving the fake socket a
    ``BlockingIOError``-forever ``recv`` and a 10 ms timeout — the
    engine's real :func:`asyncio.wait_for` does the rest.
    """
    fake = _FakeSocket(block_recv=True)
    _install_fake_socket(monkeypatch, fake)

    async def _run() -> None:
        pinger = UnprivilegedIcmpPinger()
        try:
            with pytest.raises(IcmpTimeoutError):
                await pinger.ping("192.0.2.10", payload_size=64, timeout=0.01)
        finally:
            await pinger.close()

    asyncio.run(_run())


def test_ping_raises_icmp_unreachable_error_on_destination_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fake socket returns ICMP ``type=3`` → ``IcmpUnreachableError``."""
    fake = _FakeSocket(
        recv_buffers=[
            _build_icmp_packet(type_=3, code=1, identifier=0, sequence=0),
        ],
    )
    _install_fake_socket(monkeypatch, fake)

    async def _run() -> None:
        pinger = UnprivilegedIcmpPinger()
        try:
            with pytest.raises(IcmpUnreachableError):
                await pinger.ping("192.0.2.10", payload_size=64, timeout=1.0)
        finally:
            await pinger.close()

    asyncio.run(_run())


def test_ping_raises_icmp_unreachable_error_on_oserror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``socket.getaddrinfo`` raising ``gaierror`` → ``IcmpUnreachableError``."""
    fake = _FakeSocket()
    _install_fake_socket(monkeypatch, fake)

    def _raising_getaddrinfo(*_args: Any, **_kwargs: Any) -> Any:
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", _raising_getaddrinfo)

    async def _run() -> None:
        pinger = UnprivilegedIcmpPinger()
        try:
            with pytest.raises(IcmpUnreachableError):
                await pinger.ping("192.0.2.10", payload_size=64, timeout=1.0)
        finally:
            await pinger.close()

    asyncio.run(_run())
    # The socket must not have been created when getaddrinfo fails first.
    assert fake.setblocking_called is False


def test_close_is_idempotent_and_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``close()`` may be called multiple times; internal socket is ``None`` after."""
    fake = _FakeSocket(
        recv_buffers=[_build_icmp_packet(type_=0, identifier=0, sequence=0)],
    )
    _install_fake_socket(monkeypatch, fake)

    async def _run() -> None:
        pinger = UnprivilegedIcmpPinger()
        # First successful ping creates the socket.
        await pinger.ping("192.0.2.10", payload_size=64, timeout=1.0)
        assert pinger._socket is not None  # type: ignore[attr-defined]
        assert fake.closed is False

        # First close() closes the socket and clears the reference.
        await pinger.close()
        assert pinger._socket is None  # type: ignore[attr-defined]
        assert fake.closed is True

        # Second close() is a no-op and must not raise.
        await pinger.close()
        assert pinger._socket is None  # type: ignore[attr-defined]
        assert fake.closed is True

        # Third close() (defensive) is still a no-op.
        await pinger.close()

    asyncio.run(_run())


def test_close_after_partial_failure_still_closes_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a failed ping (timeout), ``close()`` closes the socket without raising."""
    fake = _FakeSocket(block_recv=True)
    _install_fake_socket(monkeypatch, fake)

    async def _run() -> None:
        pinger = UnprivilegedIcmpPinger()
        # Ping fails with IcmpTimeoutError, but the socket was created
        # lazily *before* `wait_for` was reached.
        with pytest.raises(IcmpTimeoutError):
            await pinger.ping("192.0.2.10", payload_size=64, timeout=0.01)
        assert pinger._socket is not None  # type: ignore[attr-defined]

        # close() must clean up even after the failure.
        await pinger.close()
        assert pinger._socket is None  # type: ignore[attr-defined]
        assert fake.closed is True

    asyncio.run(_run())
