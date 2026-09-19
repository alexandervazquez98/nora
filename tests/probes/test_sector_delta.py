"""Unit tests for :func:`compute_sector_delta`.

Covers:

- ΔRTT computed exactly (ap=1 ms, sm=5 ms -> 4.0).
- ΔJitter computed exactly (ap=0.5 ms, sm=3.0 ms -> 2.5).
- ``sm_luids`` order is preserved in the return.
- Empty SM list -> empty list (caller-side guard expected).
"""

from __future__ import annotations

import pytest

from nora.probes.metrics import NodeMetrics, compute_metrics
from nora.probes.sector_delta import SectorDelta, compute_sector_delta


def _metrics(target: str, **kwargs: float | int) -> NodeMetrics:
    """Build a :class:`NodeMetrics` envelope with explicit overrides.

    The ``target`` plus the four delta-relevant fields (avg/p95 RTT,
    avg/max jitter) accept overrides; everything else defaults to 0.
    """
    return NodeMetrics(
        target=target,
        packets_transmitted=int(kwargs.get("packets_transmitted", 10)),
        packets_received=int(kwargs.get("packets_received", 10)),
        packet_loss_pct=float(kwargs.get("packet_loss_pct", 0.0)),
        drop_burst_max=int(kwargs.get("drop_burst_max", 0)),
        outage_events=int(kwargs.get("outage_events", 0)),
        rtt_min_ms=float(kwargs.get("rtt_min_ms", 0.0)),
        rtt_avg_ms=float(kwargs.get("rtt_avg_ms", 0.0)),
        rtt_median_ms=float(kwargs.get("rtt_median_ms", 0.0)),
        rtt_p95_ms=float(kwargs.get("rtt_p95_ms", 0.0)),
        rtt_max_ms=float(kwargs.get("rtt_max_ms", 0.0)),
        jitter_avg_ms=float(kwargs.get("jitter_avg_ms", 0.0)),
        jitter_max_ms=float(kwargs.get("jitter_max_ms", 0.0)),
        samples_used=int(kwargs.get("samples_used", 10)),
    )


# ---------------------------------------------------------------------------
# Δ RTT
# ---------------------------------------------------------------------------


def test_compute_sector_delta_rtt_avg_positive_when_sm_worse_than_ap() -> None:
    """AP rtt_avg=1.0, SM rtt_avg=5.0 -> ``delta_rtt_avg_ms == 4.0``."""
    ap = _metrics("ap", rtt_avg_ms=1.0, rtt_p95_ms=2.0)
    sm = _metrics("sm-001", rtt_avg_ms=5.0, rtt_p95_ms=6.0)
    deltas = compute_sector_delta(ap_metrics=ap, sm_metrics=[sm], sm_luids=["001"])
    assert len(deltas) == 1
    assert deltas[0].luid == "001"
    assert deltas[0].delta_rtt_avg_ms == pytest.approx(4.0)
    assert deltas[0].delta_rtt_p95_ms == pytest.approx(4.0)


def test_compute_sector_delta_rtt_avg_negative_when_sm_better_than_ap() -> None:
    """AP rtt_avg=10.0, SM rtt_avg=2.0 -> ``delta_rtt_avg_ms == -8.0`` (SM wins)."""
    ap = _metrics("ap", rtt_avg_ms=10.0, rtt_p95_ms=20.0)
    sm = _metrics("sm-001", rtt_avg_ms=2.0, rtt_p95_ms=3.0)
    deltas = compute_sector_delta(ap_metrics=ap, sm_metrics=[sm], sm_luids=["001"])
    assert deltas[0].delta_rtt_avg_ms == pytest.approx(-8.0)
    assert deltas[0].delta_rtt_p95_ms == pytest.approx(-17.0)


# ---------------------------------------------------------------------------
# Δ Jitter
# ---------------------------------------------------------------------------


def test_compute_sector_delta_jitter_avg_positive() -> None:
    """AP jitter_avg=0.5, SM jitter_avg=3.0 -> ``delta_jitter_avg_ms == 2.5``."""
    ap = _metrics("ap", jitter_avg_ms=0.5, jitter_max_ms=1.0)
    sm = _metrics("sm-001", jitter_avg_ms=3.0, jitter_max_ms=4.0)
    deltas = compute_sector_delta(ap_metrics=ap, sm_metrics=[sm], sm_luids=["001"])
    assert deltas[0].delta_jitter_avg_ms == pytest.approx(2.5)
    assert deltas[0].delta_jitter_max_ms == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# Ordering + empty
# ---------------------------------------------------------------------------


def test_compute_sector_delta_preserves_sm_luids_order() -> None:
    """3 SMs in ``sm_luids`` order — return order matches the input order."""
    ap = _metrics(
        "ap",
        rtt_avg_ms=1.0,
        rtt_p95_ms=2.0,
        jitter_avg_ms=0.5,
        jitter_max_ms=1.0,
    )
    # LUIDs supplied out of numeric order to catch any sort/dict leak.
    sm_luids = ["c", "a", "b"]
    sm_metrics = [
        _metrics(
            "sm-c",
            rtt_avg_ms=3.0,
            rtt_p95_ms=4.0,
            jitter_avg_ms=0.7,
            jitter_max_ms=1.5,
        ),
        _metrics(
            "sm-a",
            rtt_avg_ms=5.0,
            rtt_p95_ms=6.0,
            jitter_avg_ms=1.5,
            jitter_max_ms=2.5,
        ),
        _metrics(
            "sm-b",
            rtt_avg_ms=7.0,
            rtt_p95_ms=8.0,
            jitter_avg_ms=2.5,
            jitter_max_ms=3.5,
        ),
    ]
    deltas = compute_sector_delta(
        ap_metrics=ap,
        sm_metrics=sm_metrics,
        sm_luids=sm_luids,
    )
    assert len(deltas) == 3
    assert [d.luid for d in deltas] == sm_luids
    # Spot-check the deltas: sm-a rtt_avg=5 vs ap=1 -> 4.0; sm-b=7-1=6.0; sm-c=3-1=2.0.
    assert deltas[0].delta_rtt_avg_ms == pytest.approx(2.0)  # sm-c
    assert deltas[1].delta_rtt_avg_ms == pytest.approx(4.0)  # sm-a
    assert deltas[2].delta_rtt_avg_ms == pytest.approx(6.0)  # sm-b


def test_compute_sector_delta_empty_sm_list_returns_empty_list() -> None:
    """Empty SM list -> empty list (caller is expected to handle the zero-SM case)."""
    ap = _metrics("ap", rtt_avg_ms=1.0, rtt_p95_ms=2.0, jitter_avg_ms=0.5, jitter_max_ms=1.0)
    deltas = compute_sector_delta(ap_metrics=ap, sm_metrics=[], sm_luids=[])
    assert deltas == []


# ---------------------------------------------------------------------------
# Integration with real NodeMetrics
# ---------------------------------------------------------------------------


def test_compute_sector_delta_round_trips_through_compute_metrics() -> None:
    """Δ from metrics produced by ``compute_metrics`` matches a hand-computed Δ."""
    from nora.probes.icmp import IcmpSample

    rtts_ap = [10.0, 11.0, 10.0, 11.0, 10.0]
    rtts_sm = [50.0, 51.0, 50.0, 51.0, 50.0]
    ap_samples = [
        IcmpSample(target="ap", rtt_ms=r, received=True, error=None, timestamp_unix=0.0)
        for r in rtts_ap
    ]
    sm_samples = [
        IcmpSample(target="sm-001", rtt_ms=r, received=True, error=None, timestamp_unix=0.0)
        for r in rtts_sm
    ]
    ap = compute_metrics(ap_samples, target="ap")
    sm = compute_metrics(sm_samples, target="sm-001")
    deltas = compute_sector_delta(ap_metrics=ap, sm_metrics=[sm], sm_luids=["001"])
    assert deltas[0].delta_rtt_avg_ms == pytest.approx(40.0)
    assert isinstance(deltas[0], SectorDelta)
