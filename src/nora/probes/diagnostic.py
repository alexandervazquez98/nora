"""Sector + per-SM diagnostic verdict — issue #61 matrix.

The verdicts are a frozen ``Literal`` enum (string-valued). Branching
runs in a predictable order:

  1. SECTOR_BACKHAUL_BOTTLENECK  — AP itself shows loss > 1% or avg RTT > 50 ms.
  2. SECTOR_RF_CONGESTION       — AP is clean but >= 60% of SMs show jitter > 5 ms.
  3. per-SM verdicts (one of LOSSY_LINK / UNSTABLE_DROPS / JITTER_MODERATE /
     JITTER_CRITICAL / EXCELLENT).
  4. SECTOR verdict = ISOLATED_SUBSCRIBER_FAULT if exactly 1 SM (of N>=3)
     is non-EXCELLENT and the rest are EXCELLENT; else use a sector-level
     verdict tag derived from the per-SM spread.

Every threshold mirrors the issue #61 matrix exactly. The branch order
in :func:`classify_node` is deliberate: worst-first (UNSTABLE_DROPS →
LOSSY_LINK → JITTER_CRITICAL → JITTER_MODERATE → EXCELLENT) so a
single node that satisfies multiple clauses lands on the most-
actionable verdict.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from nora.probes.metrics import NodeMetrics
from nora.probes.sector_delta import SectorDelta, compute_sector_delta

if TYPE_CHECKING:
    from nora.probes.models import ProbeTarget


# 8 verdicts from issue #61 / WU-2.2 / WU-2.3.
DiagnosticVerdict = Literal[
    "EXCELLENT",
    "JITTER_MODERATE",
    "JITTER_CRITICAL",
    "LOSSY_LINK",
    "UNSTABLE_DROPS",
    "SECTOR_BACKHAUL_BOTTLENECK",
    "SECTOR_RF_CONGESTION",
    "ISOLATED_SUBSCRIBER_FAULT",
]


class PerSmVerdict(BaseModel):
    """One SM's diagnostic decision."""

    model_config = ConfigDict(frozen=True)

    luid: str
    """Subscriber LUID; ``""`` for the AP target if it ever lands here."""

    verdict: DiagnosticVerdict
    """The per-SM verdict — one of the 4 per-node verdicts."""

    delta: SectorDelta | None = None
    """Δ vs the AP baseline; ``None`` only when the sector had zero SMs."""


class SectorVerdict(BaseModel):
    """The full verdict envelope for one run."""

    model_config = ConfigDict(frozen=True)

    ap_verdict: DiagnosticVerdict
    per_sm: list[PerSmVerdict]
    sector_verdict: DiagnosticVerdict
    rationale: str
    """One-line, deterministic, ASCII-only summary (≤ 240 chars)."""

    evaluated_at_unix: float
    """``time.time()`` at the moment the verdict was assembled."""


def classify_node(metrics: NodeMetrics) -> DiagnosticVerdict:
    """Classify a single node's metrics into one of the 4 per-node verdicts.

    Order of evaluation matters. EXACT thresholds from issue #61 matrix:

      UNSTABLE_DROPS   packet_loss_pct > 5.0  OR  drop_burst_max >= 8
      LOSSY_LINK       1.0 <= packet_loss_pct <= 5.0
      JITTER_CRITICAL  jitter_avg_ms > 20.0
      JITTER_MODERATE  5.0 <= jitter_avg_ms <= 20.0 AND packet_loss_pct < 1.0
      EXCELLENT        everything else

    The branch order MUST be: UNSTABLE_DROPS first (worst), then
    LOSSY_LINK, then JITTER_CRITICAL, then JITTER_MODERATE, else
    EXCELLENT. A node can satisfy multiple clauses; the worst wins.
    """
    # UNSTABLE_DROPS: loss > 5% OR burst >= 8 (~8s at 1 pkt/s).
    if metrics.packet_loss_pct > 5.0 or metrics.drop_burst_max >= 8:
        return "UNSTABLE_DROPS"
    # LOSSY_LINK: 1% <= loss <= 5% (inclusive).
    if 1.0 <= metrics.packet_loss_pct <= 5.0:
        return "LOSSY_LINK"
    # JITTER_CRITICAL: jitter > 20 ms.
    if metrics.jitter_avg_ms > 20.0:
        return "JITTER_CRITICAL"
    # JITTER_MODERATE: 5 <= jitter <= 20 AND loss < 1%.
    if 5.0 <= metrics.jitter_avg_ms <= 20.0 and metrics.packet_loss_pct < 1.0:
        return "JITTER_MODERATE"
    return "EXCELLENT"


def _build_rationale(
    *,
    ap_verdict: DiagnosticVerdict,
    sector_verdict: DiagnosticVerdict,
    sm_verdicts: list[PerSmVerdict],
    backhaul_loss: float,
    backhaul_rtt: float,
) -> str:
    """Assemble the one-line rationale. ASCII-only, ≤ 240 chars.

    Format (deterministic; ``;`` separates clauses):

        ``sector=<X>; ap=<Y>; sm=<n EXCELLENT / m WORST>; reason=<key phrase>``

    Key-phrase mapping (verbatim so tests can grep on it):

        EXCELLENT                  -> "all within thresholds"
        LOSSY_LINK (ap)            -> "AP loss 1-5%"
        UNSTABLE_DROPS (ap)        -> "AP burst >= 8 or loss > 5%"
        JITTER_CRITICAL            -> "jitter > 20 ms"
        JITTER_MODERATE            -> "jitter 5-20 ms"
        SECTOR_BACKHAUL_BOTTLENECK -> "AP loss > 1% or RTT > 50 ms"
        SECTOR_RF_CONGESTION       -> ">= 60% of SMs show jitter > 5 ms"
        ISOLATED_SUBSCRIBER_FAULT  -> "exactly 1 SM degraded, others clean"
    """
    excellent = sum(1 for v in sm_verdicts if v.verdict == "EXCELLENT")
    worst = "EXCELLENT"
    worst_rank = _VERDICT_RANK.get(worst, 0)
    for v in sm_verdicts:
        r = _VERDICT_RANK.get(v.verdict, 0)
        if r > worst_rank:
            worst = v.verdict
            worst_rank = r

    if sector_verdict == "SECTOR_BACKHAUL_BOTTLENECK":
        reason = (
            f"AP loss > 1% or RTT > 50 ms (loss={backhaul_loss:.1f}%, rtt={backhaul_rtt:.1f}ms)"
        )
    elif sector_verdict == "SECTOR_RF_CONGESTION":
        reason = ">= 60% of SMs show jitter > 5 ms"
    elif sector_verdict == "ISOLATED_SUBSCRIBER_FAULT":
        reason = "exactly 1 SM degraded, others clean"
    else:
        # Map the sector verdict (or worst SM verdict) to a key phrase.
        reason = _RATIONALE_BY_VERDICT.get(sector_verdict, "all within thresholds")

    rationale = (
        f"sector={sector_verdict}; ap={ap_verdict}; "
        f"sm={excellent} EXCELLENT / {worst} worst; reason={reason}"
    )
    # Sanitizer-safe cap. We hard-truncate at 240 to be safe; the
    # typical output is well under 200 chars even for 100 SMs.
    if len(rationale) > 240:
        rationale = rationale[:237] + "..."
    # ASCII-only invariant (defensive; the format string is already ASCII).
    rationale = "".join(c for c in rationale if c.isascii())
    return rationale


# Lookups used by `_build_rationale`. Defined after the function so the
# module reads top-down.
_VERDICT_RANK: dict[str, int] = {
    "EXCELLENT": 0,
    "JITTER_MODERATE": 1,
    "LOSSY_LINK": 2,
    "JITTER_CRITICAL": 3,
    "UNSTABLE_DROPS": 4,
    # Sector-level verdicts never appear in `_VERDICT_RANK` lookups for
    # the per-SM spread — they are derived from the per-SM set, not
    # part of it. Listed here only for completeness; defaults to 0.
    "SECTOR_BACKHAUL_BOTTLENECK": 0,
    "SECTOR_RF_CONGESTION": 0,
    "ISOLATED_SUBSCRIBER_FAULT": 0,
}


_RATIONALE_BY_VERDICT: dict[str, str] = {
    "EXCELLENT": "all within thresholds",
    "JITTER_MODERATE": "jitter 5-20 ms",
    "JITTER_CRITICAL": "jitter > 20 ms",
    "LOSSY_LINK": "loss 1-5%",
    "UNSTABLE_DROPS": "burst >= 8 or loss > 5%",
}


def classify_sector(
    *,
    ap_metrics: NodeMetrics,
    ap_target: str,  # noqa: ARG001 — reserved for the rationale hook
    sm_metrics: list[NodeMetrics],
    sm_targets: list["ProbeTarget"],
    evaluated_at_unix: float,
) -> SectorVerdict:
    """Classify the sector into one of the 8 verdicts.

    Algorithm (deterministic, matches issue #61 matrix logic):

      1. AP verdict:
         - ``ap_metrics.packet_loss_pct > 1.0`` OR
           ``ap_metrics.rtt_avg_ms > 50.0`` → ``ap_verdict =
           SECTOR_BACKHAUL_BOTTLENECK`` (and ``sector_verdict`` too).
         - else → ``ap_verdict = classify_node(ap_metrics)``.

      2. Per-SM verdicts: ``classify_node(sm_metrics[i])`` for each.

      3. Sector verdict (only when step 1 was NOT backhaul):
         - If 0 SMs → ``sector_verdict = EXCELLENT`` (sector = AP-only).
         - Else if ``len(sm_metrics) >= 3`` and
           ``count(sm.jitter_avg_ms > 5.0) / len(sm_metrics) >= 0.6``
           → ``SECTOR_RF_CONGESTION``.
         - Else if exactly 1 SM (of ``>=3`` total) has verdict !=
           EXCELLENT and the others are EXCELLENT:
           → ``ISOLATED_SUBSCRIBER_FAULT``.
         - Else if any per-SM verdict is UNSTABLE_DROPS or
           JITTER_CRITICAL → that worst verdict (UNSTABLE_DROPS >
           JITTER_CRITICAL > others).
         - Else if any per-SM verdict is LOSSY_LINK or JITTER_MODERATE
           → worst of those.
         - Else → ``EXCELLENT``.

      4. Rationale: one-line ASCII string ≤ 240 chars (see
         :func:`_build_rationale`).
    """
    # Step 1: AP verdict.
    ap_verdict: DiagnosticVerdict
    sector_verdict: DiagnosticVerdict
    if ap_metrics.packet_loss_pct > 1.0 or ap_metrics.rtt_avg_ms > 50.0:
        ap_verdict = "SECTOR_BACKHAUL_BOTTLENECK"
        sector_verdict = "SECTOR_BACKHAUL_BOTTLENECK"
        per_sm_verdicts: list[PerSmVerdict] = [
            PerSmVerdict(luid=t.luid or "", verdict=classify_node(m))
            for t, m in zip(sm_targets, sm_metrics, strict=False)
        ]
        return SectorVerdict(
            ap_verdict=ap_verdict,
            per_sm=per_sm_verdicts,
            sector_verdict=sector_verdict,
            rationale=_build_rationale(
                ap_verdict=ap_verdict,
                sector_verdict=sector_verdict,
                sm_verdicts=per_sm_verdicts,
                backhaul_loss=ap_metrics.packet_loss_pct,
                backhaul_rtt=ap_metrics.rtt_avg_ms,
            ),
            evaluated_at_unix=evaluated_at_unix,
        )

    ap_verdict = classify_node(ap_metrics)

    # Step 2: per-SM verdicts + deltas.
    deltas = compute_sector_delta(
        ap_metrics=ap_metrics,
        sm_metrics=sm_metrics,
        sm_luids=[t.luid or "" for t in sm_targets],
    )
    per_sm_verdicts = [
        PerSmVerdict(luid=t.luid or "", verdict=classify_node(m), delta=d)
        for t, m, d in zip(sm_targets, sm_metrics, deltas, strict=False)
    ]

    # Step 3: sector verdict (only when the AP is clean).
    if len(sm_metrics) == 0:
        sector_verdict = "EXCELLENT"
    else:
        congested = sum(1 for m in sm_metrics if m.jitter_avg_ms > 5.0)
        if len(sm_metrics) >= 3 and (congested / len(sm_metrics)) >= 0.6:
            sector_verdict = "SECTOR_RF_CONGESTION"
        else:
            non_excellent = [v for v in per_sm_verdicts if v.verdict != "EXCELLENT"]
            if (
                len(sm_metrics) >= 3
                and len(non_excellent) == 1
                and all(
                    v.verdict == "EXCELLENT" for v in per_sm_verdicts if v is not non_excellent[0]
                )
            ):
                sector_verdict = "ISOLATED_SUBSCRIBER_FAULT"
            else:
                # Walk the per-SM verdicts in the documented priority order.
                if any(v.verdict == "UNSTABLE_DROPS" for v in per_sm_verdicts):
                    sector_verdict = "UNSTABLE_DROPS"
                elif any(v.verdict == "JITTER_CRITICAL" for v in per_sm_verdicts):
                    sector_verdict = "JITTER_CRITICAL"
                elif any(v.verdict == "LOSSY_LINK" for v in per_sm_verdicts):
                    sector_verdict = "LOSSY_LINK"
                elif any(v.verdict == "JITTER_MODERATE" for v in per_sm_verdicts):
                    sector_verdict = "JITTER_MODERATE"
                else:
                    sector_verdict = "EXCELLENT"

    return SectorVerdict(
        ap_verdict=ap_verdict,
        per_sm=per_sm_verdicts,
        sector_verdict=sector_verdict,
        rationale=_build_rationale(
            ap_verdict=ap_verdict,
            sector_verdict=sector_verdict,
            sm_verdicts=per_sm_verdicts,
            backhaul_loss=ap_metrics.packet_loss_pct,
            backhaul_rtt=ap_metrics.rtt_avg_ms,
        ),
        evaluated_at_unix=evaluated_at_unix,
    )


__all__ = [
    "DiagnosticVerdict",
    "PerSmVerdict",
    "SectorVerdict",
    "classify_node",
    "classify_sector",
]
