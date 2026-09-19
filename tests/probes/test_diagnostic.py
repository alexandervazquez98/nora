"""Unit tests for the 8-state diagnostic matrix — 100% branch coverage.

Every verdict from :data:`DiagnosticVerdict` is exercised by a
hand-built ``NodeMetrics`` (or AP + SM list) so only the branch
under test fires. The branch ordering inside
:func:`classify_node` (worst-first) is pinned by
``test_classify_node_prefers_worst_verdict_when_multiple_clauses_match``.
"""

from __future__ import annotations

from nora.probes.diagnostic import (
    DiagnosticVerdict,
    PerSmVerdict,
    SectorVerdict,
    classify_node,
    classify_sector,
)
from nora.probes.metrics import NodeMetrics
from nora.probes.models import ProbeTarget


def _metrics(
    target: str,
    *,
    packet_loss_pct: float = 0.0,
    drop_burst_max: int = 0,
    rtt_avg_ms: float = 10.0,
    jitter_avg_ms: float = 0.5,
) -> NodeMetrics:
    """Build a :class:`NodeMetrics` envelope with the relevant fields set.

    Unspecified fields default to "healthy" so each test only has to
    express the field that actually drives the verdict.
    """
    return NodeMetrics(
        target=target,
        packets_transmitted=100,
        packets_received=int(100 - packet_loss_pct),
        packet_loss_pct=packet_loss_pct,
        drop_burst_max=drop_burst_max,
        outage_events=0,
        rtt_min_ms=1.0,
        rtt_avg_ms=rtt_avg_ms,
        rtt_median_ms=2.0,
        rtt_p95_ms=3.0,
        rtt_max_ms=4.0,
        jitter_avg_ms=jitter_avg_ms,
        jitter_max_ms=1.0,
        samples_used=100,
    )


# ---------------------------------------------------------------------------
# classify_node — one test per verdict
# ---------------------------------------------------------------------------


def test_classify_node_excellent() -> None:
    """Loss=0, RTT=2, jitter=1 -> EXCELLENT."""
    assert classify_node(_metrics("h", rtt_avg_ms=2.0, jitter_avg_ms=1.0)) == "EXCELLENT"


def test_classify_node_jitter_moderate() -> None:
    """Loss=0, jitter=8 (5<=j<=20 AND loss<1) -> JITTER_MODERATE."""
    assert classify_node(_metrics("h", jitter_avg_ms=8.0, rtt_avg_ms=15.0)) == "JITTER_MODERATE"


def test_classify_node_jitter_critical() -> None:
    """jitter=25 (>20) -> JITTER_CRITICAL (worst-of: not loss)."""
    assert classify_node(_metrics("h", jitter_avg_ms=25.0)) == "JITTER_CRITICAL"


def test_classify_node_lossy_link() -> None:
    """loss=2 (1<=loss<=5) -> LOSSY_LINK."""
    assert classify_node(_metrics("h", packet_loss_pct=2.0)) == "LOSSY_LINK"


def test_classify_node_unstable_drops() -> None:
    """loss=6 (>5) OR burst=10 (>=8) -> UNSTABLE_DROPS."""
    assert classify_node(_metrics("h", packet_loss_pct=6.0)) == "UNSTABLE_DROPS"
    # Branch via drop_burst_max alone (loss=0).
    assert classify_node(_metrics("h", drop_burst_max=10, packet_loss_pct=0.0)) == "UNSTABLE_DROPS"


# ---------------------------------------------------------------------------
# classify_sector — one test per sector verdict
# ---------------------------------------------------------------------------


def test_classify_sector_backhaul_bottleneck() -> None:
    """AP has loss=2 (>1) -> ``ap_verdict`` and ``sector_verdict`` are both
    SECTOR_BACKHAUL_BOTTLENECK."""
    ap = _metrics("ap", packet_loss_pct=2.0)
    sector = classify_sector(
        ap_metrics=ap,
        ap_target="ap",
        sm_metrics=[],
        sm_targets=[],
        evaluated_at_unix=0.0,
    )
    assert sector.ap_verdict == "SECTOR_BACKHAUL_BOTTLENECK"
    assert sector.sector_verdict == "SECTOR_BACKHAUL_BOTTLENECK"
    assert sector.per_sm == []


def test_classify_sector_backhaul_bottleneck_via_rtt_threshold() -> None:
    """AP has rtt_avg=60 (>50) -> SECTOR_BACKHAUL_BOTTLENECK via the RTT branch."""
    ap = _metrics("ap", rtt_avg_ms=60.0, jitter_avg_ms=0.5, packet_loss_pct=0.0)
    sector = classify_sector(
        ap_metrics=ap,
        ap_target="ap",
        sm_metrics=[],
        sm_targets=[],
        evaluated_at_unix=0.0,
    )
    assert sector.sector_verdict == "SECTOR_BACKHAUL_BOTTLENECK"


def test_classify_sector_rf_congestion() -> None:
    """AP clean, 3/3 SMs with jitter=10 (>5) -> SECTOR_RF_CONGESTION."""
    ap = _metrics("ap", jitter_avg_ms=1.0)
    sm_metrics = [_metrics(f"sm-{n}", jitter_avg_ms=10.0) for n in ("a", "b", "c")]
    sm_targets = [ProbeTarget(role="sm", luid=n, host=n, label=f"SM {n}") for n in ("a", "b", "c")]
    sector = classify_sector(
        ap_metrics=ap,
        ap_target="ap",
        sm_metrics=sm_metrics,
        sm_targets=sm_targets,
        evaluated_at_unix=0.0,
    )
    assert sector.sector_verdict == "SECTOR_RF_CONGESTION"


def test_classify_sector_isolated_subscriber_fault() -> None:
    """AP clean, 3 SMs (2 EXCELLENT + 1 JITTER_MODERATE) -> ISOLATED_SUBSCRIBER_FAULT."""
    ap = _metrics("ap", jitter_avg_ms=0.5)
    sm_metrics = [
        _metrics("sm-a", jitter_avg_ms=1.0),
        _metrics("sm-b", jitter_avg_ms=8.0),  # JITTER_MODERATE
        _metrics("sm-c", jitter_avg_ms=1.0),
    ]
    sm_targets = [ProbeTarget(role="sm", luid=n, host=n, label=f"SM {n}") for n in ("a", "b", "c")]
    sector = classify_sector(
        ap_metrics=ap,
        ap_target="ap",
        sm_metrics=sm_metrics,
        sm_targets=sm_targets,
        evaluated_at_unix=0.0,
    )
    assert sector.sector_verdict == "ISOLATED_SUBSCRIBER_FAULT"
    assert isinstance(sector, SectorVerdict)


# ---------------------------------------------------------------------------
# Branch-order / edge cases
# ---------------------------------------------------------------------------


def test_classify_node_prefers_worst_verdict_when_multiple_clauses_match() -> None:
    """loss=6, jitter=25 -> UNSTABLE_DROPS (not JITTER_CRITICAL) — UNSTABLE_DROPS first."""
    assert classify_node(_metrics("h", packet_loss_pct=6.0, jitter_avg_ms=25.0)) == "UNSTABLE_DROPS"


def test_classify_sector_zero_sm_returns_excellent() -> None:
    """Empty SM list -> sector_verdict=EXCELLENT (sector = AP-only)."""
    ap = _metrics("ap", jitter_avg_ms=1.0)
    sector = classify_sector(
        ap_metrics=ap,
        ap_target="ap",
        sm_metrics=[],
        sm_targets=[],
        evaluated_at_unix=0.0,
    )
    assert sector.sector_verdict == "EXCELLENT"
    assert sector.ap_verdict == "EXCELLENT"


def test_classify_sector_widest_lossy_spread_picks_lossy_link() -> None:
    """3 SMs: 1 EXCELLENT + 2 LOSSY_LINK -> sector_verdict = LOSSY_LINK."""
    ap = _metrics("ap", jitter_avg_ms=1.0)
    sm_metrics = [
        _metrics("sm-a", jitter_avg_ms=1.0),
        _metrics("sm-b", packet_loss_pct=3.0),  # LOSSY_LINK
        _metrics("sm-c", packet_loss_pct=3.0),  # LOSSY_LINK
    ]
    sm_targets = [ProbeTarget(role="sm", luid=n, host=n, label=f"SM {n}") for n in ("a", "b", "c")]
    sector = classify_sector(
        ap_metrics=ap,
        ap_target="ap",
        sm_metrics=sm_metrics,
        sm_targets=sm_targets,
        evaluated_at_unix=0.0,
    )
    assert sector.sector_verdict == "LOSSY_LINK"


def test_classify_sector_unstable_drops_wins_over_jitter_critical() -> None:
    """3 SMs: 1 UNSTABLE_DROPS + 1 JITTER_CRITICAL -> sector_verdict = UNSTABLE_DROPS."""
    ap = _metrics("ap", jitter_avg_ms=1.0)
    sm_metrics = [
        _metrics("sm-a", packet_loss_pct=7.0),  # UNSTABLE_DROPS
        _metrics("sm-b", jitter_avg_ms=25.0),  # JITTER_CRITICAL
        _metrics("sm-c", jitter_avg_ms=1.0),
    ]
    sm_targets = [ProbeTarget(role="sm", luid=n, host=n, label=f"SM {n}") for n in ("a", "b", "c")]
    sector = classify_sector(
        ap_metrics=ap,
        ap_target="ap",
        sm_metrics=sm_metrics,
        sm_targets=sm_targets,
        evaluated_at_unix=0.0,
    )
    assert sector.sector_verdict == "UNSTABLE_DROPS"


def test_rationale_is_ascii_and_under_240_chars() -> None:
    """Every verdict's rationale is ASCII-only and <= 240 chars."""
    ap_loss = _metrics("ap", packet_loss_pct=2.0)  # BACKHAUL
    ap_clean = _metrics("ap", jitter_avg_ms=1.0)
    sm_metrics_rf = [_metrics(f"sm-{n}", jitter_avg_ms=10.0) for n in ("a", "b", "c")]
    sm_targets_rf = [
        ProbeTarget(role="sm", luid=n, host=n, label=f"SM {n}") for n in ("a", "b", "c")
    ]
    sm_metrics_isolated = [
        _metrics("sm-a", jitter_avg_ms=1.0),
        _metrics("sm-b", jitter_avg_ms=8.0),  # JITTER_MODERATE
        _metrics("sm-c", jitter_avg_ms=1.0),
    ]
    sm_targets_isolated = [
        ProbeTarget(role="sm", luid=n, host=n, label=f"SM {n}") for n in ("a", "b", "c")
    ]
    sm_metrics_lossy = [
        _metrics("sm-a", packet_loss_pct=3.0),
        _metrics("sm-b", packet_loss_pct=3.0),
        _metrics("sm-c", jitter_avg_ms=1.0),
    ]
    sm_targets_lossy = [
        ProbeTarget(role="sm", luid=n, host=n, label=f"SM {n}") for n in ("a", "b", "c")
    ]

    scenarios: list[NodeMetrics | tuple[NodeMetrics, list[NodeMetrics], list[ProbeTarget]]] = [
        (ap_loss, [], []),  # backhaul
        (ap_clean, [], []),  # zero SM (EXCELLENT)
        (ap_clean, sm_metrics_rf, sm_targets_rf),  # RF_CONGESTION
        (ap_clean, sm_metrics_isolated, sm_targets_isolated),  # ISOLATED
        (ap_clean, sm_metrics_lossy, sm_targets_lossy),  # LOSSY_LINK
    ]

    for scenario in scenarios:
        if isinstance(scenario, tuple):
            ap_m, sm_m, sm_t = scenario
        else:
            ap_m, sm_m, sm_t = scenario, [], []
        sector = classify_sector(
            ap_metrics=ap_m,
            ap_target="ap",
            sm_metrics=sm_m,
            sm_targets=sm_t,
            evaluated_at_unix=0.0,
        )
        rationale = sector.rationale
        assert isinstance(rationale, str)
        assert len(rationale) <= 240, f"rationale too long ({len(rationale)} chars): {rationale!r}"
        assert all(c.isascii() for c in rationale), (
            f"rationale contains non-ASCII chars: {rationale!r}"
        )


def test_rationale_contains_verdict_keyword() -> None:
    """The rationale mentions the verdict name (operator grep)."""
    ap = _metrics("ap", packet_loss_pct=2.0)
    sector = classify_sector(
        ap_metrics=ap,
        ap_target="ap",
        sm_metrics=[],
        sm_targets=[],
        evaluated_at_unix=0.0,
    )
    assert "SECTOR_BACKHAUL_BOTTLENECK" in sector.rationale


# ---------------------------------------------------------------------------
# Type guards
# ---------------------------------------------------------------------------


def test_per_sm_verdict_delta_attached_for_isolated_subscriber_fault() -> None:
    """``PerSmVerdict.delta`` is a SectorDelta for every non-empty SM list."""
    ap = _metrics("ap", jitter_avg_ms=1.0)
    sm_metrics = [
        _metrics("sm-a", jitter_avg_ms=1.0),
        _metrics("sm-b", jitter_avg_ms=8.0),
        _metrics("sm-c", jitter_avg_ms=1.0),
    ]
    sm_targets = [ProbeTarget(role="sm", luid=n, host=n, label=f"SM {n}") for n in ("a", "b", "c")]
    sector = classify_sector(
        ap_metrics=ap,
        ap_target="ap",
        sm_metrics=sm_metrics,
        sm_targets=sm_targets,
        evaluated_at_unix=0.0,
    )
    assert all(isinstance(v, PerSmVerdict) for v in sector.per_sm)
    assert all(v.delta is not None for v in sector.per_sm)

    # DiagnosticVerdict literal membership sanity.
    for verdict in (
        "EXCELLENT",
        "JITTER_MODERATE",
        "JITTER_CRITICAL",
        "LOSSY_LINK",
        "UNSTABLE_DROPS",
        "SECTOR_BACKHAUL_BOTTLENECK",
        "SECTOR_RF_CONGESTION",
        "ISOLATED_SUBSCRIBER_FAULT",
    ):
        assert verdict in DiagnosticVerdict.__args__, f"{verdict} missing from Literal"
