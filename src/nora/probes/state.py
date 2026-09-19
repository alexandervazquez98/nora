"""In-process registry for in-flight ICMP probe runs.

The MCP tool bodies are synchronous, but ``run_probe`` is an async
generator. We bridge them with a daemon :class:`threading.Thread`
that owns a private ``asyncio`` event loop and drives the coordinator
to completion; the registry holds the live :class:`RunState` snapshot
behind a :class:`threading.Lock` so the MCP wrappers can read it
from any thread.

State is process-local; if the process restarts, all in-flight runs
are lost (acceptable for PR1 — PR2 persistence will harden it).

Wiring
======

The FastMCP tool body runs in the sync handler thread; the daemon
that drives :func:`nora.probes.probe.run_probe` runs in its own
thread with its own event loop. The two never share a loop, so we
do NOT need to worry about ``asyncio.run()`` being called from a
running loop.

Concurrency
-----------

The registry uses ``threading.Lock`` (NOT :class:`asyncio.Lock`)
because the MCP tool bodies are synchronous — there is no event loop
on the reader side. ``RunState`` carries its own
``threading.Lock`` so the daemon can append samples safely while the
sync wrapper reads snapshots.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from nora.probes.icmp import IcmpSample

logger = logging.getLogger(__name__)


class RunStatus:
    """Status enum — string literals (Pydantic-friendly).

    The registry stays out of the probes.models package to avoid a
    cycle; consumers map the literal to a Pydantic enum in PR2.
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


@dataclass
class RunState:
    """Live snapshot of a probe run, mutated under ``lock``.

    ``lock`` is a per-run :class:`threading.Lock` so the daemon thread
    can append samples while a sync MCP wrapper reads a snapshot
    concurrently. ``lock`` is held briefly (append + status read) so
    contention stays negligible.
    """

    run_id: str
    device_id: str
    ap_host: str
    started_at_unix: float
    samples: list[IcmpSample] = field(default_factory=list)
    status: str = RunStatus.PENDING
    finished_at_unix: float | None = None
    last_error: str | None = None
    result_summary: dict[str, Any] | None = None
    """PR2 (issue #61): the post-completion ``ProbeRunCompleted`` envelope
    serialised via ``model_dump(mode='json')``. Populated by the daemon's
    ``else:`` branch after the iterator exhausts. ``None`` until the
    analysis completes (or fails — in which case the failure lands on
    ``last_error`` and ``result_summary`` stays ``None``).
    """
    lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self, *, sample_window: int = 100) -> dict[str, Any]:
        """Return a JSON-serialisable snapshot for the MCP response.

        ``sample_window`` caps the size of the ``last_samples`` field
        so a long-running poll does not ship the entire buffer every
        time. The default is 100 samples, mirroring the PR1 spec.

        The PR2 ``result_summary`` field is exposed only when set so
        PR1 consumers see a stable shape.
        """
        with self.lock:
            snap: dict[str, Any] = {
                "run_id": self.run_id,
                "device_id": self.device_id,
                "ap_host": self.ap_host,
                "status": self.status,
                "started_at_unix": self.started_at_unix,
                "finished_at_unix": self.finished_at_unix,
                "samples_count": len(self.samples),
                "last_samples": [s.model_dump(mode="json") for s in self.samples[-sample_window:]],
                "last_error": self.last_error,
            }
            if self.result_summary is not None:
                snap["result_summary"] = self.result_summary
            return snap


class ProbeRunRegistry:
    """In-memory registry of live probe runs.

    Thread-safe (uses :class:`threading.Lock`). The lock is held only
    for short read / write operations; long-running work lives on
    :class:`_RunDaemon` threads.
    """

    def __init__(self, *, ttl_seconds: int = 3600) -> None:
        self._runs: dict[str, RunState] = {}
        self._lock = threading.Lock()
        self._ttl_seconds = ttl_seconds

    def register(self, state: RunState) -> None:
        with self._lock:
            self._sweep_expired_unlocked()
            self._runs[state.run_id] = state

    def get(self, run_id: str) -> RunState | None:
        with self._lock:
            return self._runs.get(run_id)

    def cancel(self, run_id: str) -> bool:
        """Request cancellation; returns ``True`` iff the run was found.

        Sets the per-run status to ``CANCELLED`` (atomic with the
        lock acquisition). The daemon's drain loop checks the status
        on every sample flush and exits its ``async for`` when it
        flips — the cancellation propagates without a separate
        :class:`asyncio.Event`.
        """
        with self._lock:
            state = self._runs.get(run_id)
            if state is None:
                return False
            with state.lock:
                state.status = RunStatus.CANCELLED
                state.finished_at_unix = time.time()
            return True

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Read-only JSON-serialisable view of every run — used by tests."""
        with self._lock:
            return {rid: state.snapshot() for rid, state in self._runs.items()}

    def _sweep_expired_unlocked(self) -> None:
        """Remove runs older than ``ttl_seconds``. Called under ``_lock``."""
        cutoff = time.time() - self._ttl_seconds
        to_remove = [rid for rid, s in self._runs.items() if s.started_at_unix < cutoff]
        for rid in to_remove:
            del self._runs[rid]


# ---------------------------------------------------------------------------
# Daemon driver — wires the async generator to a thread.
# ---------------------------------------------------------------------------


class _RunDaemon:
    """Owns one :class:`asyncio` event loop + thread + ``run_probe`` task.

    Lifecycle: :meth:`start` spawns the thread; the thread runs the
    loop until the iterator completes (or fails); :meth:`join` blocks
    the caller until the thread exits.

    The MCP tool body holds a reference to this daemon and queries
    ``registry.get(run_id)`` for progress — it does not interact with
    the daemon directly.
    """

    def __init__(
        self,
        *,
        registry: ProbeRunRegistry,
        run_state: RunState,
        driver: Any,
        device_id: str,
        settings: Any,
        duration_seconds: int,
        interval_seconds: float,
        packet_size_bytes: int,
        per_packet_timeout_seconds: float,
        target_luids: list[str] | None,
    ) -> None:
        self._registry = registry
        self._run_state = run_state
        self._driver = driver
        self._device_id = device_id
        self._settings = settings
        self._duration = duration_seconds
        self._interval = interval_seconds
        self._payload = packet_size_bytes
        self._per_pkt_timeout = per_packet_timeout_seconds
        self._target_luids = target_luids
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run_in_thread,
            name=f"probe-runner-{self._run_state.run_id}",
            daemon=True,
        )
        self._thread.start()

    def join(self, *, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _run_in_thread(self) -> None:
        """Thread entry point: build a private loop, drive the run."""
        self._loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._drive())
        except Exception as exc:  # noqa: BLE001 — top-of-loop guard
            logger.exception(
                "probes.daemon.unhandled",
                extra={
                    "event": "probes.daemon.unhandled",
                    "run_id": self._run_state.run_id,
                },
            )
            with self._run_state.lock:
                if self._run_state.status == RunStatus.RUNNING:
                    self._run_state.status = RunStatus.FAILED
                self._run_state.last_error = repr(exc)
                self._run_state.finished_at_unix = time.time()
        finally:
            self._loop.close()
            self._loop = None

    async def _drive(self) -> None:
        """Async body — drives :func:`run_probe` and updates ``RunState`."""
        from nora.probes.probe import ProbeConfigurationError, run_probe

        with self._run_state.lock:
            self._run_state.status = RunStatus.RUNNING

        try:
            iterator = run_probe(
                driver=self._driver,
                device_id=self._device_id,
                settings=self._settings,
                duration_seconds=self._duration,
                interval_seconds=self._interval,
                packet_size_bytes=self._payload,
                per_packet_timeout_seconds=self._per_pkt_timeout,
                target_luids=self._target_luids,
            )
            async for sample in iterator:
                with self._run_state.lock:
                    if self._run_state.status == RunStatus.CANCELLED:
                        # Consumer-side cancellation: stop iterating.
                        break
                    self._run_state.samples.append(sample)
            else:
                # Loop exited cleanly (iterator exhausted).
                with self._run_state.lock:
                    self._run_state.status = RunStatus.COMPLETED
                    self._run_state.finished_at_unix = time.time()
                # PR2 (issue #61) post-completion hook — fold the
                # collected samples into metrics, classify the
                # sector, persist the result, and attach the
                # serialised ProbeRunCompleted envelope to
                # ``self._run_state.result_summary``. The hook is
                # lazy-imported to keep the PR1 surface free of the
                # PR2 module graph, and wrapped in try/except so a
                # post-completion failure never flips the run from
                # COMPLETED back to FAILED (the data is on disk in
                # self._run_state.samples; the analysis is a
                # best-effort aggregation step).
                await self._attach_post_completion_result()
        except ProbeConfigurationError as exc:
            with self._run_state.lock:
                self._run_state.status = RunStatus.FAILED
                self._run_state.last_error = str(exc)
                self._run_state.finished_at_unix = time.time()
        except asyncio.CancelledError:
            with self._run_state.lock:
                self._run_state.status = RunStatus.CANCELLED
                self._run_state.finished_at_unix = time.time()
            raise
        except Exception as exc:  # noqa: BLE001 — surface to MCP
            with self._run_state.lock:
                self._run_state.status = RunStatus.FAILED
                self._run_state.last_error = repr(exc)
                self._run_state.finished_at_unix = time.time()

    async def _attach_post_completion_result(self) -> None:
        """Fold the run's samples into a ``ProbeRunCompleted`` envelope.

        PR2 (issue #61) runs after the iterator exhausts:

        1. Bucket samples by target → one ``NodeMetrics`` per target.
        2. Split the bucket into AP + SMs using ``self._run_state.ap_host``.
        3. ``classify_sector(...)`` → ``SectorVerdict``.
        4. ``save_probe_run(...)`` → atomic JSON under
           ``settings.nora_probe_results_dir``.
        5. ``ProbeRunCompleted(...).model_dump(mode="json")`` → stored
           on ``self._run_state.result_summary`` under the lock.

        Any exception in this hook is logged + swallowed; the run
        stays COMPLETED and ``last_error`` is left untouched (the
        samples themselves are still on ``RunState.samples`` so a
        future retry is possible).
        """
        # Lazy imports — keep the PR1 surface free of the PR2 module
        # graph. The PR2 modules in turn re-export the underlying
        # primitives (``IcmpSample`` / ``NodeMetrics`` / etc.) so we
        # only depend on the runners.
        from nora.probes.diagnostic import classify_sector
        from nora.probes.metrics import NodeMetrics, compute_metrics
        from nora.probes.models import (
            ProbeRunCompleted,
            ProbeRunSummary,
            ProbeTarget,
        )
        from nora.probes.persistence import save_probe_run
        from nora.probes.sector_delta import compute_sector_delta

        try:
            samples = list(self._run_state.samples)
            if not samples:
                return

            # Bucket by target. The coordinator emits one bucket per
            # destination, so we just group on the ``target`` field.
            buckets: dict[str, list[IcmpSample]] = {}
            for sample in samples:
                buckets.setdefault(sample.target, []).append(sample)

            ap_target = self._run_state.ap_host
            if ap_target not in buckets:
                logger.warning(
                    "probes.post_completion.no_ap_samples",
                    extra={
                        "event": "probes.post_completion.no_ap_samples",
                        "run_id": self._run_state.run_id,
                        "ap_host": ap_target,
                    },
                )
                return

            ap_metrics = compute_metrics(buckets[ap_target], target=ap_target)

            # Build ordered SM (target, metrics) pairs in LUID order.
            # PR2 has no separate LUID table — the coordinator emits
            # the AP host for the AP bucket and SM hosts (== LUIDs in
            # the PR1 placeholder) for everything else. Sort
            # lexicographically for determinism.
            sm_pairs: list[tuple[str, NodeMetrics]] = sorted(
                (target, compute_metrics(items, target=target))
                for target, items in buckets.items()
                if target != ap_target
            )

            # SectorDelta needs an explicit sm_luids list; we use the
            # SM host (== LUID in PR1 placeholder) as the LUID. ProbeTarget
            # is purely a typed shape for the diagnostic API.
            sm_metrics_list = [m for _, m in sm_pairs]
            sm_targets = [
                ProbeTarget(role="sm", luid=t, host=t, label=f"SM {t}") for t, _ in sm_pairs
            ]
            sm_luids = [t for t, _ in sm_pairs]

            sector_delta = compute_sector_delta(
                ap_metrics=ap_metrics,
                sm_metrics=sm_metrics_list,
                sm_luids=sm_luids,
            )

            # classify_sector attaches the deltas to per-SM verdicts
            # itself, but we ALSO need the sector_delta list in the
            # summary. Both envelopes are derived from the same
            # inputs so recomputing here is cheap.
            summary = ProbeRunSummary(
                ap_metrics=ap_metrics,
                sm_metrics=sm_metrics_list,
                sector_delta=sector_delta,
            )

            evaluated_at_unix = time.time()
            verdict = classify_sector(
                ap_metrics=ap_metrics,
                ap_target=ap_target,
                sm_metrics=sm_metrics_list,
                sm_targets=sm_targets,
                evaluated_at_unix=evaluated_at_unix,
            )

            # Build the persistence payload.
            payload: dict[str, Any] = {
                "run_id": self._run_state.run_id,
                "sector": "nora",
                "device_id": self._run_state.device_id,
                "ap_ip": ap_target,
                "started_at_unix": self._run_state.started_at_unix,
                "finished_at_unix": evaluated_at_unix,
                "metrics_summary": summary.model_dump(mode="json"),
                "verdict": verdict.model_dump(mode="json"),
            }
            persisted = save_probe_run(self._settings, payload)

            envelope = ProbeRunCompleted(
                run_id=self._run_state.run_id,
                metrics=summary,
                verdict=verdict,
                persisted=persisted,
                analyzed_at_unix=evaluated_at_unix,
            )
            with self._run_state.lock:
                self._run_state.result_summary = envelope.model_dump(mode="json")
        except Exception as exc:  # noqa: BLE001 — best-effort, never fails the run
            logger.warning(
                "probes.post_completion.failed",
                extra={
                    "event": "probes.post_completion.failed",
                    "run_id": self._run_state.run_id,
                    "error": repr(exc),
                },
                exc_info=True,
            )


# ---------------------------------------------------------------------------
# Public driver function — `start_probe_run`.
# ---------------------------------------------------------------------------


def start_probe_run(
    *,
    registry: ProbeRunRegistry,
    driver: Any,
    device_id: str,
    settings: Any,
    run_id: str,
    duration_seconds: int,
    interval_seconds: float,
    packet_size_bytes: int,
    per_packet_timeout_seconds: float,
    ap_host: str,
    target_luids: list[str] | None,
) -> RunState:
    """Register the run and spawn its daemon. Returns the :class:`RunState`.

    The returned object is also registered in ``registry`` and will
    receive samples as the daemon progresses. The MCP tool body polls
    ``registry.get(run_id).snapshot()`` for progress.
    """
    state = RunState(
        run_id=run_id,
        device_id=device_id,
        ap_host=ap_host,
        started_at_unix=time.time(),
    )
    registry.register(state)
    daemon = _RunDaemon(
        registry=registry,
        run_state=state,
        driver=driver,
        device_id=device_id,
        settings=settings,
        duration_seconds=duration_seconds,
        interval_seconds=interval_seconds,
        packet_size_bytes=packet_size_bytes,
        per_packet_timeout_seconds=per_packet_timeout_seconds,
        target_luids=target_luids,
    )
    daemon.start()
    return state


__all__ = ["ProbeRunRegistry", "RunState", "RunStatus", "start_probe_run"]
