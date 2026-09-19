"""Per-node metrics aggregation for the ICMP stability probe (issue #61 PR2).

The single public function ``compute_metrics(samples) -> NodeMetrics``
folds a per-target sample stream into the typed `NodeMetrics` envelope:

- packet_loss_pct, drop_burst_max, outage_events
- RTT min / avg / median / p95 / max
- jitter_avg_ms, jitter_max_ms (RFC 3550 J[i] = J[i-1] + (|D(i-1,i)| - J[i-1]) / 16)

The module is pure (no I/O, no logging, no time mutation). Every branch
is pinned by ``tests/probes/test_metrics.py``.

RFC 3550 jitter
===============

RFC 3550 §A.8 (the RTP/RTCP reference) defines an interarrival-jitter
estimate with a 16-tap smoothing filter::

    J[i] = J[i-1] + (|D(i-1, i)| - J[i-1]) / 16

where ``D(i-1, i)`` is the difference between consecutive round-trip
delays. The function applies the same recurrence to the ICMP RTT
stream — jitter_max is the peak observed J[i], jitter_avg is the mean
of every J[i] computed (one per consecutive received pair). The
reference RFC is ``RFC 3550 - RTP: A Transport Protocol for Real-Time
Applications`` §6.4.1 / §A.8.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict

from nora.probes.icmp import IcmpSample


class NodeMetrics(BaseModel):
    """Per-target metrics — folded from one IcmpSample stream.

    All fields are required. Empty / all-loss samples produce zero-
    valued metrics (rather than ``None``) so the diagnostic layer can
    read the envelope without optional-handling.
    """

    model_config = ConfigDict(frozen=True)

    target: str
    """IPv4 literal (or LUID string) the samples were addressed to."""

    packets_transmitted: int
    """``len(samples)`` — every sample the engine emitted, received or not."""

    packets_received: int
    """Count of samples with ``received=True`` (regardless of ``rtt_ms``)."""

    packet_loss_pct: float
    """``(tx - rx) / tx * 100.0`` (or 0.0 when ``tx == 0``)."""

    drop_burst_max: int
    """Longest consecutive run of ``received=False`` samples."""

    outage_events: int
    """Count of contiguous ``received=False`` runs of length >= 3."""

    rtt_min_ms: float
    rtt_avg_ms: float
    rtt_median_ms: float
    rtt_p95_ms: float
    rtt_max_ms: float

    jitter_avg_ms: float
    """Mean of every J[i] across consecutive received pairs (0.0 when none)."""

    jitter_max_ms: float
    """Peak observed J[i] (0.0 when no consecutive received pairs exist)."""

    samples_used: int
    """Count of received samples that contributed to the RTT stats."""


def compute_metrics(samples: Sequence[IcmpSample], *, target: str | None = None) -> NodeMetrics:
    """Fold one per-target sample stream into typed NodeMetrics.

    Args:
        samples: One destination's worth of samples (pre-filtered by target).
        target: Optional override for the returned ``target`` field. If omitted,
            derived from ``samples[0].target`` (all samples in ``samples``
            MUST share the same target — the coordinator guarantees this by
            running one ping loop per destination). If ``samples`` is empty,
            the returned envelope's ``target`` is ``target or ""``.

    Returns:
        A frozen :class:`NodeMetrics` envelope. Empty / all-loss inputs
        produce zero-valued RTT and jitter fields so the diagnostic
        layer can union them without optional-handling.

    Algorithm:
        1. ``packets_transmitted = len(samples)``.
        2. ``received_samples = [s for s in samples if s.received]``.
        3. ``packets_received = len(received_samples)``.
        4. ``packet_loss_pct = (tx - rx) / tx * 100.0``, or ``0.0`` if ``tx == 0``.
        5. RTT stats are computed only on samples with ``received=True``
           and ``rtt_ms is not None``. ``min`` / ``avg`` / ``median`` use
           the stdlib; ``p95_ms`` uses ``statistics.quantiles(rtts, n=20,
           method="inclusive")[18]`` (the 19th of 20 inclusive quantiles
           between 0 and 1 — this is the conventional inclusive-quantile
           definition that lands at p95 for inputs of size >= 20). If
           ``len(rtts) < 2``, p95 degenerates to ``max(rtts)``. If
           ``len(rtts) == 0``, every RTT field is 0.0.
        6. ``drop_burst_max`` is the longest consecutive run of
           ``received=False`` samples (zero on an empty stream).
        7. ``outage_events`` counts the contiguous ``received=False``
           runs of length ``>= 3``. (At the default 1 packet / second
           cadence this corresponds to ``>= 3 s`` of continuous loss;
           callers on a different cadence can recompute on the side.)
        8. Jitter — RFC 3550 §A.8. The first received sample initialises
           ``J = 0``. For each subsequent pair ``(s_prev, s_curr)`` where
           both are received::

               D = abs(s_curr.rtt_ms - s_prev.rtt_ms)
               J = J + (D - J) / 16.0

           ``jitter_avg_ms`` is the mean of every J (one per pair) or
           ``0.0`` if no pair exists; ``jitter_max_ms`` is the maximum J
           or ``0.0``. Samples with ``received=False`` between two
           received samples break the consecutive-pair chain — the
           algorithm only walks the contiguous received sub-runs.

    Pure function; no I/O, no logging, no time mutation. Tests use
    hand-built sample sequences to pin every branch.
    """
    tx = len(samples)

    # Step 0: empty stream -> zero-valued metrics. We still honour the
    # optional `target` override so callers can tag an empty result.
    if tx == 0:
        return NodeMetrics(
            target=target if target is not None else "",
            packets_transmitted=0,
            packets_received=0,
            packet_loss_pct=0.0,
            drop_burst_max=0,
            outage_events=0,
            rtt_min_ms=0.0,
            rtt_avg_ms=0.0,
            rtt_median_ms=0.0,
            rtt_p95_ms=0.0,
            rtt_max_ms=0.0,
            jitter_avg_ms=0.0,
            jitter_max_ms=0.0,
            samples_used=0,
        )

    # Step 1+2+3: tx / rx counts.
    received_samples = [s for s in samples if s.received]
    rx = len(received_samples)
    packet_loss_pct = ((tx - rx) / tx) * 100.0

    # Step 5: RTT stats. We restrict to received samples whose `rtt_ms`
    # is not None — the engine sets `rtt_ms=None` on every failed
    # sample, so the intersection is normally just `received_samples`,
    # but the defensive guard keeps a stale-typed payload from crashing.
    rtts = [s.rtt_ms for s in received_samples if s.rtt_ms is not None]
    samples_used = len(rtts)

    if samples_used == 0:
        rtt_min_ms = 0.0
        rtt_avg_ms = 0.0
        rtt_median_ms = 0.0
        rtt_p95_ms = 0.0
        rtt_max_ms = 0.0
    else:
        rtt_min_ms = min(rtts)
        rtt_avg_ms = sum(rtts) / samples_used
        rtt_median_ms = statistics.median(rtts)
        # p95 via statistics.quantiles: with n=20 the inclusive method
        # returns 19 cut points between 0 and 1, so index 18 is the
        # 19th quantile (≈ p95 / 0.95). For inputs of size < 2 the
        # quantile function raises; we fall back to `max(rtts)` so a
        # single-sample stream still produces a usable percentile.
        if samples_used < 2:
            rtt_p95_ms = rtt_max_ms = max(rtts)
        else:
            quantiles = statistics.quantiles(rtts, n=20, method="inclusive")
            rtt_p95_ms = quantiles[18]
            rtt_max_ms = max(rtts)

    # Step 6+7: drop-burst + outage counts. The two are co-located so
    # they share one linear scan.
    drop_burst_max = 0
    outage_events = 0
    current_run = 0
    for s in samples:
        if not s.received:
            current_run += 1
            continue
        # Close-out on a received sample: tally the run we just ended.
        if current_run > 0:
            if current_run > drop_burst_max:
                drop_burst_max = current_run
            if current_run >= 3:
                outage_events += 1
            current_run = 0
    # Trailing run (samples may end on a loss sequence).
    if current_run > 0:
        if current_run > drop_burst_max:
            drop_burst_max = current_run
        if current_run >= 3:
            outage_events += 1

    # Step 8: RFC 3550 jitter. Walk the received sub-runs so an
    # `received=False` sample between two received samples does NOT
    # contribute to the inter-arrival D.
    jitter_values: list[float] = []
    j = 0.0
    prev_rtt: float | None = None
    for s in received_samples:
        rtt = s.rtt_ms
        if rtt is None:
            # Defensive — `received_samples` already filters, but a
            # future engine variant might emit `received=True, rtt_ms=None`.
            continue
        if prev_rtt is None:
            # First received sample in this contiguous received run:
            # initialise J=0 (no D to compute against the previous
            # packet because there wasn't one in this run).
            j = 0.0
            jitter_values.append(j)
        else:
            d = abs(rtt - prev_rtt)
            j = j + (d - j) / 16.0
            jitter_values.append(j)
        prev_rtt = rtt
        if not s.received:  # unreachable branch; keeps mypy strict-friendly.
            prev_rtt = None

    if jitter_values:
        jitter_avg_ms = sum(jitter_values) / len(jitter_values)
        jitter_max_ms = max(jitter_values)
    else:
        jitter_avg_ms = 0.0
        jitter_max_ms = 0.0

    # Resolve the `target` label. Prefer the explicit override; fall
    # back to `samples[0].target` so the returned envelope is always
    # self-describing.
    resolved_target = target if target is not None else samples[0].target

    return NodeMetrics(
        target=resolved_target,
        packets_transmitted=tx,
        packets_received=rx,
        packet_loss_pct=packet_loss_pct,
        drop_burst_max=drop_burst_max,
        outage_events=outage_events,
        rtt_min_ms=rtt_min_ms,
        rtt_avg_ms=rtt_avg_ms,
        rtt_median_ms=rtt_median_ms,
        rtt_p95_ms=rtt_p95_ms,
        rtt_max_ms=rtt_max_ms,
        jitter_avg_ms=jitter_avg_ms,
        jitter_max_ms=jitter_max_ms,
        samples_used=samples_used,
    )


__all__ = ["NodeMetrics", "compute_metrics"]
