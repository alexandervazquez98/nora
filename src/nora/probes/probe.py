"""Async probe coordinator for the ICMP sector stability probe (issue #61).

This module owns the *pacing* of the unprivileged ICMP engine
(:mod:`nora.probes.icmp`). The coordinator is a single async generator
that yields one :class:`nora.probes.icmp.IcmpSample` per destination
per ``interval_seconds``, paced by per-target asyncio tasks, drained
through a bounded :class:`asyncio.Queue`. The generator exits cleanly
on the duration deadline, on consumer ``break``, and on
``CancelledError``; the ``finally:`` clause always cancels every
per-target task and closes every pinger.

The PR1 MCP wrapper (WU-1.5) and the PR2 metrics aggregator (WU-2.1)
consume the iterator verbatim — they never touch the engine directly.

Pacing model
============

One task per destination, each running an independent paced loop:

* The task sleeps until ``next_send`` (monotonic clock).
* On wake-up it sends exactly one packet via the destination's
  pinger, then increments ``next_send`` by ``interval``. Missed
  schedules accumulate as drift (next packet fires immediately) —
  preferable to per-task catch-up bursts.
* Per-packet failures are translated into a ``received=False`` sample
  so the consumer sees a continuous stream even on lossy links. The
  typed engine exceptions (``IcmpTimeoutError``,
  ``IcmpUnreachableError``, ``IcmpEngineError``) are caught at the
  loop boundary — packet loss is *data*, not a runtime error.
* The task exits when ``stop_event`` is set or when ``CancelledError``
  propagates from ``asyncio.Queue.put`` (back-pressure cancellation).

Cancellation
============

``asyncio.CancelledError`` propagates through ``yield`` and the
``finally:`` clause is guaranteed to run. The clause:

1. Sets ``stop_event`` so every per-target task wakes up immediately.
2. Cancels every per-target task and awaits them with
   ``return_exceptions=True`` (cancels cannot be silently lost).
3. Closes every pinger via its idempotent ``close()``.

This makes the coordinator safe to embed inside a parent task that
the MCP wrapper cancels when its HTTP timeout fires (decision #9 in
``odd/tasks/issue-61-icmp-stability-probe.md``).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from nora.probes.discovery import discover_targets
from nora.probes.exceptions import (
    IcmpEngineError,
    IcmpTimeoutError,
    IcmpUnreachableError,
)
from nora.probes.icmp import IcmpPinger, IcmpSample, UnprivilegedIcmpPinger
from nora.probes.models import ProbeRunSettings, ProbeRunStarted, ProbeTarget

if TYPE_CHECKING:
    from nora.config import Settings

__all__ = [
    "ProbeConfigurationError",
    "generate_run_id",
    "run_probe",
]


logger = logging.getLogger(__name__)


class ProbeConfigurationError(ValueError):
    """Invalid runtime parameters for ``run_probe()`` (out-of-bounds duration, etc.).

    Distinct from a Pydantic ``ValidationError`` so the MCP wrapper
    (WU-1.5) can map it to a structured MCP error envelope without
    parsing string messages.
    """


def generate_run_id() -> str:
    """Return an 8-char hex string suitable as a probe-run identifier.

    Uses ``secrets.token_hex(4)`` — 32 bits of randomness.
    Cryptographically secure; collisions across a sector (typical
    < 200 SMs + 1 AP) are astronomically improbable.
    """
    return secrets.token_hex(4)


def _resolve_run_settings(
    *,
    settings: "Settings",
    duration_seconds: int | None,
    interval_seconds: float | None,
    packet_size_bytes: int | None,
    per_packet_timeout_seconds: float | None,
) -> tuple[int, float, int, float]:
    """Apply Settings defaults for ``None`` arguments; return the effective tuple.

    Centralises the default-merge so bounds validation operates on the
    same numbers the run will actually use.
    """
    duration = (
        duration_seconds
        if duration_seconds is not None
        else settings.nora_icmp_default_duration_seconds
    )
    interval = (
        interval_seconds
        if interval_seconds is not None
        else settings.nora_icmp_default_interval_seconds
    )
    payload_size = (
        packet_size_bytes
        if packet_size_bytes is not None
        else settings.nora_icmp_default_packet_size_bytes
    )
    per_pkt_timeout = (
        per_packet_timeout_seconds
        if per_packet_timeout_seconds is not None
        else settings.nora_icmp_per_packet_timeout_seconds
    )
    return duration, interval, payload_size, per_pkt_timeout


def _validate_run_settings(
    *,
    settings: "Settings",
    duration: int,
    interval: float,
    payload_size: int,
    per_pkt_timeout: float,
) -> None:
    """Raise :class:`ProbeConfigurationError` on any out-of-bounds parameter.

    Called BEFORE the discovery helper so a misconfigured call never
    spends a single byte of bandwidth on the wire. The bounds mirror
    ``Settings._validate_icmp_duration_bounds``; the coordinator
    enforces them again at the run boundary because the operator may
    pass arbitrary kwargs that bypass Settings validation.
    """
    if duration < settings.nora_icmp_min_duration_seconds:
        raise ProbeConfigurationError(
            f"duration_seconds={duration} below min={settings.nora_icmp_min_duration_seconds}"
        )
    if duration > settings.nora_icmp_max_duration_seconds:
        raise ProbeConfigurationError(
            f"duration_seconds={duration} above max={settings.nora_icmp_max_duration_seconds}"
        )
    if interval <= 0:
        raise ProbeConfigurationError(f"interval_seconds={interval} must be > 0")
    if payload_size <= 0:
        raise ProbeConfigurationError(f"packet_size_bytes={payload_size} must be > 0")
    if per_pkt_timeout <= 0:
        raise ProbeConfigurationError(f"per_packet_timeout_seconds={per_pkt_timeout} must be > 0")


async def run_probe(
    *,
    driver: Any,
    device_id: str,
    settings: "Settings",
    duration_seconds: int | None = None,
    interval_seconds: float | None = None,
    packet_size_bytes: int | None = None,
    per_packet_timeout_seconds: float | None = None,
    target_luids: list[str] | None = None,
) -> AsyncIterator[IcmpSample]:
    """Async generator: yields one IcmpSample per destination per packet.

    Pacing — one packet per destination per ``interval_seconds``.
    Duration — stops cleanly after ``duration_seconds`` (or on
    ``CancelledError``). Cancellation triggers a ``finally:`` clause
    that cancels every per-target task and closes every pinger.

    All Optional[number] parameters default to the corresponding
    Settings field. Bounds checking (``min <= duration <= max``,
    ``interval > 0``, ``packet_size_bytes > 0``, ``per_packet_timeout > 0``)
    happens BEFORE the discovery call so a misconfigured call never
    reaches the wire.

    Note: ``target_luids`` is propagated to :func:`discover_targets` so
    the operator's filter reaches the discovery layer.
    ``PRE_EXISTING_OFFLINE`` SMs are filtered before the coordinator
    ever spawns a pinger for them, so the probe bandwidth stays
    bounded.
    """
    # 1. Apply settings defaults for None values.
    duration, interval, payload_size, per_pkt_timeout = _resolve_run_settings(
        settings=settings,
        duration_seconds=duration_seconds,
        interval_seconds=interval_seconds,
        packet_size_bytes=packet_size_bytes,
        per_packet_timeout_seconds=per_packet_timeout_seconds,
    )

    # 2. Bounds validation. Raise ProbeConfigurationError BEFORE any
    #    discovery or wire activity so misconfigured calls never
    #    spend bandwidth.
    _validate_run_settings(
        settings=settings,
        duration=duration,
        interval=interval,
        payload_size=payload_size,
        per_pkt_timeout=per_pkt_timeout,
    )

    # 3. Resolve the live discovery (PRE_EXISTING_OFFLINE excluded).
    discovery = discover_targets(
        driver=driver,
        device_id=device_id,
        settings=settings,
        target_luids=target_luids,
    )
    targets = discovery.all_targets  # [AP, *SMs_sorted_by_luid]

    # If nothing to probe (only AP exists or all SMs excluded), still
    # log and let the duration timer drive the yield loop. The
    # generator yields nothing in that case but completes cleanly.
    run_id = generate_run_id()
    started = ProbeRunStarted(
        run_id=run_id,
        device_id=device_id,
        ap_host=discovery.ap_host,
        started_at_unix=time.time(),
        settings=ProbeRunSettings(
            duration_seconds=duration,
            interval_seconds=interval,
            packet_size_bytes=payload_size,
            per_packet_timeout_seconds=per_pkt_timeout,
        ),
    )
    logger.info(
        "probes.run.start",
        extra={
            "event": "probes.run.start",
            "run_id": run_id,
            "device_id": device_id,
            "ap_count": 1,
            "sm_count": len(discovery.sm_targets),
            "duration_seconds": duration,
            "interval_seconds": interval,
        },
    )

    # 4. Spin one UnprivilegedIcmpPinger per destination. The pingers
    #    are owned by THIS generator; cancellation must close them all.
    pingers: list[UnprivilegedIcmpPinger] = [
        UnprivilegedIcmpPinger(per_packet_timeout_seconds=per_pkt_timeout) for _ in targets
    ]

    sample_queue: asyncio.Queue[IcmpSample] = asyncio.Queue(maxsize=10_000)
    stop_event = asyncio.Event()

    async def _ping_loop(
        pinger: IcmpPinger,
        target: ProbeTarget,
    ) -> None:
        """Drive the per-destination paced ping loop.

        Pacing: never send faster than 1 packet / ``interval_seconds``.
        Exception handling: ``ping()`` errors translate to failed
        samples (``received=False, error=str``) so the iterator's
        consumer sees a continuous stream even on lossy links. The
        exception is NOT re-raised — per-packet loss is data, not a
        runtime error. The per-target loop exits cleanly when
        ``stop_event`` is set.
        """
        next_send = time.monotonic()
        try:
            while not stop_event.is_set():
                now = time.monotonic()
                sleep_for = next_send - now
                if sleep_for > 0:
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=sleep_for)
                        return  # stop requested during sleep
                    except asyncio.TimeoutError:
                        pass  # interval elapsed, send next packet
                try:
                    sample = await pinger.ping(
                        target.host,
                        payload_size=payload_size,
                        timeout=per_pkt_timeout,
                    )
                except IcmpTimeoutError as exc:
                    sample = IcmpSample(
                        target=target.host,
                        rtt_ms=None,
                        received=False,
                        error=str(exc),
                        timestamp_unix=time.time(),
                    )
                except IcmpUnreachableError as exc:
                    sample = IcmpSample(
                        target=target.host,
                        rtt_ms=None,
                        received=False,
                        error=str(exc),
                        timestamp_unix=time.time(),
                    )
                except IcmpEngineError as exc:
                    # Any other engine-level error (e.g. socket shut).
                    # Treat as a failed sample and continue.
                    sample = IcmpSample(
                        target=target.host,
                        rtt_ms=None,
                        received=False,
                        error=str(exc),
                        timestamp_unix=time.time(),
                    )
                try:
                    await sample_queue.put(sample)
                except asyncio.CancelledError:
                    return
                next_send += interval
        except asyncio.CancelledError:
            return

    # 5. Spawn one task per target. `started` is exposed via the local
    #    scope so future WUs can attach metrics to the run snapshot;
    #    the PR1 surface does not consume it yet.
    _ = started
    ping_tasks: list[asyncio.Task[None]] = [
        asyncio.create_task(_ping_loop(pingers[i], target)) for i, target in enumerate(targets)
    ]

    # 6. Drain the queue and yield. The drain loop exits when:
    #    - the duration_seconds deadline elapses,
    #    - the caller breaks out of the iterator,
    #    - the caller cancels the outer task.
    deadline = time.monotonic() + duration
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                sample = await asyncio.wait_for(
                    sample_queue.get(), timeout=min(remaining, interval)
                )
            except asyncio.TimeoutError:
                # No sample arrived within `interval`; check deadline
                # and loop. If the deadline has expired, the outer
                # condition exits on the next iteration.
                continue
            yield sample
    finally:
        # 7. Clean shutdown — guaranteed to run on normal exit, break,
        #    exception, AND CancelledError propagation from the caller.
        stop_event.set()
        for task in ping_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*ping_tasks, return_exceptions=True)
        for pinger in pingers:
            await pinger.close()
        logger.info(
            "probes.run.end",
            extra={
                "event": "probes.run.end",
                "run_id": run_id,
                "device_id": device_id,
            },
        )
