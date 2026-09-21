"""Unprivileged ICMP echo engine for the issue #61 sector stability probe.

This module ships the *transport* the sector stability probe is built on. It
opens one :class:`socket.socket` (``AF_INET`` / ``SOCK_DGRAM`` /
``IPPROTO_ICMP``) per :class:`UnprivilegedIcmpPinger` instance (lazy on the
first :meth:`~UnprivilegedIcmpPinger.ping` call) and assembles ICMP echo
request packets manually with a 16-bit identifier (derived from
:func:`os.getpid`) and a rolling 16-bit sequence counter.

The engine relies on Linux's unprivileged-ICMP datagram path: the kernel
will accept ``sendto()`` on a ``SOCK_DGRAM`` + ``IPPROTO_ICMP`` socket when
the caller's gid falls inside ``net.ipv4.ping_group_range``. Operators wire
the sysctl in ``INSTALL.md`` (PR3). Raw sockets (``SOCK_RAW``) are
deliberately avoided here because systemd's ``PrivateDevices=true`` and the
repo-wide air-gap AST guard ban ``socket`` imports inside
``src/nora/drivers/`` and ``src/nora/prompts/``. This module lives under
``src/nora/probes/`` so the ban does not apply, but the cleaner
``SOCK_DGRAM`` path is preferred for portability regardless.

ICMP checksum
=============

The standard RFC 1071 Internet checksum is computed by
:func:`_compute_icmp_checksum`.

Verified test vector (echo request, ``id=1``, ``seq=0``, no payload,
checksum field placeholder = ``0``)::

    Input bytes: b"\\x08\\x00\\x00\\x00\\x00\\x01\\x00\\x00"
    Expected:    0xf7fe

Derivation (ones-complement sum of 16-bit big-endian words)::

    0x0800 + 0x0000 + 0x0001 + 0x0000 = 0x0801
    ones-complement of 0x0801          = 0xf7fe
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import struct
import time
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from nora.probes.exceptions import (
    IcmpTimeoutError,
    IcmpUnreachableError,
)

__all__ = [
    "IcmpPinger",
    "IcmpSample",
    "UnprivilegedIcmpPinger",
]


logger = logging.getLogger(__name__)


# ICMP type constants used by the engine (RFC 792).
_ICMP_TYPE_ECHO_REPLY = 0
_ICMP_TYPE_DEST_UNREACHABLE = 3
_ICMP_TYPE_TIME_EXCEEDED = 11

# ICMP header is 8 bytes (type, code, checksum, id, seq).
_ICMP_HEADER_LEN = 8
_ICMP_ECHO_TYPE = 8
_ICMP_ECHO_CODE = 0

# Maximum ICMP datagram we accept on `recv` (the engine does not parse past
# the header for the reply path; oversized buffers just waste memory).
_RECV_BUFSIZE = 4096


class IcmpSample(BaseModel):
    """A single ICMP echo verdict (one round-trip or one typed failure)."""

    model_config = ConfigDict(frozen=True)

    target: str
    """IPv4 literal that the pinger probed."""

    rtt_ms: float | None
    """Round-trip time in milliseconds; ``None`` iff ``received`` is False."""

    received: bool
    """True when an ICMP echo reply arrived within ``timeout``."""

    error: str | None
    """Typed exception class name plus a one-line message, or None on success."""

    timestamp_unix: float
    """``time.time()`` at the moment the verdict was known."""


class IcmpPinger(Protocol):
    """The minimal contract the probe coordinator (WU-1.3) consumes.

    Implementations must raise the typed exceptions from
    :mod:`nora.probes.exceptions` on failure, and
    :meth:`close` must be idempotent and never raise.
    """

    async def ping(
        self,
        target: str,
        *,
        payload_size: int,
        timeout: float,
    ) -> IcmpSample: ...

    async def close(self) -> None: ...


def _compute_icmp_checksum(data: bytes) -> int:
    """Standard RFC 1071 Internet checksum over ICMP header + payload.

    The caller MUST pre-set the checksum field to zero before invoking.

    Test vector (echo request, id=1, seq=0, no payload, checksum placeholder=0)::

        Input bytes: b"\\x08\\x00\\x00\\x00\\x00\\x01\\x00\\x00"
        Output:      0xf7fe
    """
    padded = data if len(data) % 2 == 0 else data + b"\x00"
    total = 0
    for i in range(0, len(padded), 2):
        total += (padded[i] << 8) | padded[i + 1]
    # Fold the 32-bit accumulator into 16 bits (ones-complement sum).
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def _build_echo_request(
    *,
    identifier: int,
    sequence: int,
    payload_size: int,
) -> bytes:
    """Assemble an ICMP echo request packet with the checksum pre-filled.

    Header layout (RFC 792)::

        byte  0:   type     = 8 (echo request)
        byte  1:   code     = 0
        bytes 2-3: checksum = ones-complement Internet checksum
        bytes 4-5: identifier
        bytes 6-7: sequence number
        bytes 8+:  payload  = ``payload_size`` bytes of 0x00
    """
    if payload_size < 0:
        raise ValueError("payload_size must be non-negative")
    if payload_size > 0xFFFF:
        raise ValueError("payload_size must fit in a single ICMP datagram")
    ident = identifier & 0xFFFF
    seq = sequence & 0xFFFF
    header_placeholder = struct.pack(
        "!BBHHH",
        _ICMP_ECHO_TYPE,
        _ICMP_ECHO_CODE,
        0,
        ident,
        seq,
    )
    payload = b"\x00" * payload_size
    checksum = _compute_icmp_checksum(header_placeholder + payload)
    header = struct.pack(
        "!BBHHH",
        _ICMP_ECHO_TYPE,
        _ICMP_ECHO_CODE,
        checksum,
        ident,
        seq,
    )
    return header + payload


class UnprivilegedIcmpPinger:
    """Linux unprivileged ICMP echo pinger.

    Uses one :class:`socket.socket` (``AF_INET`` / ``SOCK_DGRAM`` /
    ``IPPROTO_ICMP``) held for the pinger's lifetime and reused across
    many :meth:`ping` calls. Thread-safety is not required: one pinger
    per destination, called from one task at a time.
    """

    def __init__(self, *, per_packet_timeout_seconds: float = 5.0) -> None:
        if per_packet_timeout_seconds <= 0:
            raise ValueError("per_packet_timeout_seconds must be > 0")
        self._per_packet_timeout_seconds = per_packet_timeout_seconds
        self._socket: socket.socket | None = None
        self._sequence = 0  # rolling 16-bit counter

    @property
    def per_packet_timeout_seconds(self) -> float:
        """The default per-packet timeout this pinger was constructed with."""
        return self._per_packet_timeout_seconds

    async def close(self) -> None:
        """Idempotent, never-raises socket shutdown.

        Safe to call multiple times: the second call is a no-op. Any
        exception raised by ``socket.close()`` is swallowed so callers
        can rely on this method in ``finally`` blocks.
        """
        sock = self._socket
        self._socket = None
        if sock is None:
            return
        try:
            sock.close()
        except Exception:  # noqa: BLE001 — close() must never raise.
            logger.debug("icmp.socket.close.swallowed", exc_info=True)

    async def ping(
        self,
        target: str,
        *,
        payload_size: int,
        timeout: float,
    ) -> IcmpSample:
        """Send one ICMP echo request and return the verdict.

        Raises:
            IcmpTimeoutError: the echo reply did not arrive within
                ``timeout`` seconds.
            IcmpUnreachableError: ``target`` could not be resolved, the
                socket could not be opened (e.g. ``ping_group_range``
                excludes the caller's gid), or the kernel reported a
                destination-unreachable / time-exceeded reply.
        """
        if timeout <= 0:
            raise ValueError("timeout must be > 0")
        if payload_size < 0 or payload_size > 0xFFFF:
            raise ValueError("payload_size must be in 0..65535")

        # 1. Resolve target. We accept IPv4 literals; DNS failures land here.
        try:
            socket.getaddrinfo(target, 0)
        except socket.gaierror as exc:
            raise IcmpUnreachableError(
                f"could not resolve {target!r}: {exc.__class__.__name__}: {exc}"
            ) from exc

        # 2. Ensure the ICMP datagram socket exists *before* `wait_for`
        #    so `close()` can clean up even after a per-packet timeout.
        await self._ensure_socket()

        # 3. Build the ICMP echo request packet.
        sequence = self._sequence
        self._sequence = (self._sequence + 1) & 0xFFFF
        packet = _build_echo_request(
            identifier=os.getpid() & 0xFFFF,
            sequence=sequence,
            payload_size=payload_size,
        )

        # 4. Send + recv with an explicit timeout.
        try:
            sample = await asyncio.wait_for(
                self._send_and_recv(target, packet),
                timeout=timeout,
            )
        except asyncio.TimeoutError as exc:
            logger.warning(
                "icmp.ping.timeout",
                extra={
                    "event": "icmp.error",
                    "target": target,
                    "error": "IcmpTimeoutError",
                },
            )
            raise IcmpTimeoutError(f"ICMP echo to {target!r} timed out after {timeout}s") from exc
        except OSError as exc:
            logger.warning(
                "icmp.ping.oserror",
                extra={
                    "event": "icmp.error",
                    "target": target,
                    "error": exc.__class__.__name__,
                },
            )
            raise IcmpUnreachableError(
                f"ICMP echo to {target!r} failed: {exc.__class__.__name__}: {exc}"
            ) from exc

        logger.info(
            "icmp.ping.ok",
            extra={
                "event": "icmp.sample",
                "target": target,
                "rtt_ms": sample.rtt_ms,
            },
        )
        return sample

    async def _send_and_recv(self, target: str, packet: bytes) -> IcmpSample:
        sock = await self._ensure_socket()
        loop = asyncio.get_running_loop()
        send_mono = time.monotonic()
        await loop.sock_sendto(sock, packet, (target, 0))
        data = await loop.sock_recv(sock, _RECV_BUFSIZE)
        recv_mono = time.monotonic()

        # 5. Parse the ICMP reply.
        if len(data) < _ICMP_HEADER_LEN:
            raise IcmpUnreachableError(f"short ICMP reply from {target!r}: {len(data)} bytes")
        icmp_type = data[0]
        icmp_code = data[1]
        if icmp_type == _ICMP_TYPE_ECHO_REPLY:
            rtt_ms = (recv_mono - send_mono) * 1000.0
            return IcmpSample(
                target=target,
                rtt_ms=rtt_ms,
                received=True,
                error=None,
                timestamp_unix=time.time(),
            )
        if icmp_type in (_ICMP_TYPE_DEST_UNREACHABLE, _ICMP_TYPE_TIME_EXCEEDED):
            raise IcmpUnreachableError(
                f"ICMP unreachable from {target!r}: type={icmp_type}, code={icmp_code}"
            )
        raise IcmpUnreachableError(
            f"unexpected ICMP reply from {target!r}: type={icmp_type}, code={icmp_code}"
        )

    async def _ensure_socket(self) -> socket.socket:
        existing = self._socket
        if existing is not None:
            return existing
        try:
            sock = socket.socket(
                socket.AF_INET,
                socket.SOCK_DGRAM,
                socket.IPPROTO_ICMP,
            )
        except OSError as exc:
            raise IcmpUnreachableError(
                f"failed to open ICMP datagram socket: {exc.__class__.__name__}: {exc}"
            ) from exc
        sock.setblocking(False)
        self._socket = sock
        return sock
