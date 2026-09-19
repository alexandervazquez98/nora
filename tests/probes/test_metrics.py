"""Unit tests for ``compute_metrics()`` — RFC 3550 jitter + percentiles.

Covers:

- Empty samples -> zero-valued metrics.
- All ``received=False`` -> zero RTT, zero jitter.
- Single received sample -> RTT=single_value, jitter=0.
- RFC 3550 jitter on a hand-built sequence (worker computes the
  expected J values and asserts exactly; documents the 16-tap
  smoothing in the test docstring).
- p95 via ``statistics.quantiles(n=20, method="inclusive")`` index 18.
- ``drop_burst_max`` + ``outage_events`` counted across a hand-built
  sequence.
"""

from __future__ import annotations

import statistics
import time

import pytest

from nora.probes.icmp import IcmpSample
from nora.probes.metrics import NodeMetrics, compute_metrics

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _sample(
    target: str,
    *,
    rtt_ms: float | None,
    received: bool,
    error: str | None = None,
    timestamp_unix: float = 0.0,
) -> IcmpSample:
    """Build an :class:`IcmpSample` with sensible defaults for tests."""
    return IcmpSample(
        target=target,
        rtt_ms=rtt_ms,
        received=received,
        error=error,
        timestamp_unix=timestamp_unix or time.time(),
    )


def _received(target: str, rtt_ms: float) -> IcmpSample:
    return _sample(target, rtt_ms=rtt_ms, received=True)


def _loss(target: str, error: str = "timeout") -> IcmpSample:
    return _sample(target, rtt_ms=None, received=False, error=error)


# ---------------------------------------------------------------------------
# Empty / degenerate inputs
# ---------------------------------------------------------------------------


def test_compute_metrics_empty_samples_returns_zero_valued_envelope() -> None:
    """Empty input -> ``NodeMetrics`` with all zero-valued fields, target=''."""
    metrics = compute_metrics([])
    assert isinstance(metrics, NodeMetrics)
    assert metrics.target == ""
    assert metrics.packets_transmitted == 0
    assert metrics.packets_received == 0
    assert metrics.packet_loss_pct == 0.0
    assert metrics.drop_burst_max == 0
    assert metrics.outage_events == 0
    assert metrics.rtt_min_ms == 0.0
    assert metrics.rtt_avg_ms == 0.0
    assert metrics.rtt_median_ms == 0.0
    assert metrics.rtt_p95_ms == 0.0
    assert metrics.rtt_max_ms == 0.0
    assert metrics.jitter_avg_ms == 0.0
    assert metrics.jitter_max_ms == 0.0
    assert metrics.samples_used == 0


def test_compute_metrics_explicit_target_override_for_empty_input() -> None:
    """``target=...`` overrides the empty-input default (``""``)."""
    metrics = compute_metrics([], target="ap-norte-7400")
    assert metrics.target == "ap-norte-7400"
    assert metrics.packets_transmitted == 0


def test_compute_metrics_all_loss_samples_zero_rtt_and_jitter() -> None:
    """``received=False`` for every sample -> RTT and jitter are zero; loss = 100%."""
    samples = [_loss("192.0.2.10") for _ in range(7)]
    metrics = compute_metrics(samples, target="192.0.2.10")
    assert metrics.packets_transmitted == 7
    assert metrics.packets_received == 0
    assert metrics.packet_loss_pct == 100.0
    assert metrics.rtt_avg_ms == 0.0
    assert metrics.rtt_p95_ms == 0.0
    assert metrics.jitter_avg_ms == 0.0
    assert metrics.jitter_max_ms == 0.0
    assert metrics.samples_used == 0
    # 7 consecutive losses -> drop_burst_max = 7, outage_events = 1.
    assert metrics.drop_burst_max == 7
    assert metrics.outage_events == 1


def test_compute_metrics_single_received_sample_rtt_equals_sample_value() -> None:
    """One received sample: min=avg=median=p95=max=that value; jitter=0."""
    samples = [_received("192.0.2.10", 3.0)]
    metrics = compute_metrics(samples, target="192.0.2.10")
    assert metrics.packets_transmitted == 1
    assert metrics.packets_received == 1
    assert metrics.packet_loss_pct == 0.0
    assert metrics.rtt_min_ms == 3.0
    assert metrics.rtt_avg_ms == 3.0
    assert metrics.rtt_median_ms == 3.0
    assert metrics.rtt_p95_ms == 3.0  # degenerate: max(rtts)
    assert metrics.rtt_max_ms == 3.0
    assert metrics.jitter_avg_ms == 0.0
    assert metrics.jitter_max_ms == 0.0
    assert metrics.samples_used == 1


# ---------------------------------------------------------------------------
# Mixed received / loss
# ---------------------------------------------------------------------------


def test_compute_metrics_loss_burst_and_outage_events() -> None:
    """Drop-burst + outage events across a hand-built sequence.

    Pattern: R R R L L L L L L L L R R R (3 runs of consecutive
    ``received=False``, of lengths 9, 0, 0 — only one run qualifies as
    an outage because the threshold is ``>= 3``).

    Actually the chosen pattern is: R R L L L L L L L L R R R —
    drop_burst_max = 8, outage_events = 1 (the single run of 8
    consecutive losses exceeds the 3-sample threshold).
    """
    samples = (
        [_received("h", 1.0)] * 2
        + [_loss("h")] * 8  # burst of 8 losses
        + [_received("h", 1.0)] * 3
    )
    metrics = compute_metrics(samples, target="h")
    assert metrics.drop_burst_max == 8
    assert metrics.outage_events == 1
    # 5 received out of 13 -> loss ≈ 61.538% (computed by hand below).
    assert metrics.packets_received == 5
    assert metrics.packet_loss_pct == pytest.approx((13 - 5) / 13 * 100.0)


def test_compute_metrics_two_outage_events_when_two_bursts_above_threshold() -> None:
    """Two separate runs of length 4 each -> 2 outage events, drop_burst_max = 4."""
    samples = (
        [_received("h", 1.0)]
        + [_loss("h")] * 4
        + [_received("h", 1.0)]
        + [_loss("h")] * 4
        + [_received("h", 1.0)]
    )
    metrics = compute_metrics(samples, target="h")
    assert metrics.drop_burst_max == 4
    assert metrics.outage_events == 2


def test_compute_metrics_p95_via_quantiles_inclusive_index_18() -> None:
    """p95 is ``statistics.quantiles(rtts, n=20, method='inclusive')[18]``.

    We build 20 monotonically-increasing rtts so the inclusive
    quantile at index 18 is a known, hand-computable value.
    """
    rtts = [float(i + 1) for i in range(20)]  # 1.0 .. 20.0
    samples = [_received("h", r) for r in rtts]
    metrics = compute_metrics(samples, target="h")

    expected_quantiles = statistics.quantiles(rtts, n=20, method="inclusive")
    expected_p95 = expected_quantiles[18]
    # Sanity: for a 1..20 monotonic sequence, p95 is between 18 and 20.
    assert 18.0 <= expected_p95 <= 20.0, f"unexpected p95 sanity: {expected_p95}"
    assert metrics.rtt_p95_ms == pytest.approx(expected_p95)


# ---------------------------------------------------------------------------
# RFC 3550 jitter — hand-computed
# ---------------------------------------------------------------------------


def test_compute_metrics_rfc3550_jitter_hand_computed() -> None:
    """RFC 3550 §A.8 — J[i] = J[i-1] + (|D(i-1,i)| - J[i-1]) / 16.

    Sequence: rtts = [10.0, 12.0, 11.0, 30.0, 29.0]

    Walk (initial J=0 on first sample):
      Sample 0  rtt=10  -> J = 0.0       (no previous in this run)
      Sample 1  rtt=12  -> D = |12-10|=2 -> J = 0 + (2 - 0)/16 = 0.125
      Sample 2  rtt=11  -> D = |11-12|=1 -> J = 0.125 + (1-0.125)/16 = 0.1796875
      Sample 3  rtt=30  -> D = |30-11|=19-> J = 0.1796875 + (19-0.1796875)/16
              ≈ 1.35595703125
      Sample 4  rtt=29  -> D = |29-30|=1 -> J = 1.35595703125 + (1-1.35595703125)/16
              ≈ 1.333709716796875

    max = 1.35595703125
    avg = (0 + 0.125 + 0.1796875 + 1.35595703125 + 1.333709716796875) / 5

    The test asserts each value exactly so a future regression in the
    smoothing constant surfaces immediately.
    """
    rtts = [10.0, 12.0, 11.0, 30.0, 29.0]
    samples = [_received("h", r) for r in rtts]
    metrics = compute_metrics(samples, target="h")

    expected_js = [
        0.0,
        0.125,
        0.1796875,
        1.35595703125,
        1.333709716796875,
    ]
    expected_max = max(expected_js)
    expected_avg = sum(expected_js) / len(expected_js)

    assert metrics.jitter_max_ms == pytest.approx(expected_max)
    assert metrics.jitter_avg_ms == pytest.approx(expected_avg)


def test_compute_metrics_jitter_skips_loss_samples() -> None:
    """A ``received=False`` between two received samples is SKIPPED, not a reset.

    Per the WU-2.1 spec: "jitter computed only across consecutive
    received pairs (skipping over any ``received=False`` samples
    between them)". The D recurrence continues from the last
    received sample; the loss sample is dropped from the iterator.

    Sequence: R(10), R(12), L, R(50), R(50)
      R(10):  j=0                                                           (initial)
      R(12):  D=2,   j = 0 + 2/16                       = 0.125
      L:      (skipped — not in received_samples)
      R(50):  D=|50-12|=38, j = 0.125 + (38-0.125)/16    = 2.4921875
      R(50):  D=0,    j = 2.4921875 + (0-2.4921875)/16  = 2.33642578125

    max = 2.4921875
    avg = (0 + 0.125 + 2.4921875 + 2.33642578125) / 4
    """
    samples = [
        _received("h", 10.0),
        _received("h", 12.0),
        _loss("h"),
        _received("h", 50.0),
        _received("h", 50.0),
    ]
    metrics = compute_metrics(samples, target="h")
    assert metrics.jitter_max_ms == pytest.approx(2.4921875)
    expected_avg = (0.0 + 0.125 + 2.4921875 + 2.33642578125) / 4
    assert metrics.jitter_avg_ms == pytest.approx(expected_avg)


# ---------------------------------------------------------------------------
# Sample stream derived from samples[0].target
# ---------------------------------------------------------------------------


def test_compute_metrics_target_defaults_to_samples_first_target() -> None:
    """When ``target`` is omitted, the returned envelope uses ``samples[0].target``."""
    samples = [_received("192.0.2.99", 1.0)]
    metrics = compute_metrics(samples)
    assert metrics.target == "192.0.2.99"
