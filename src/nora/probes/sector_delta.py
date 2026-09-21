"""Sector-level deltas — ΔRTT and ΔJitter per SM vs the AP.

The deltas isolate the RF hop from the backhaul/core latency:
  ΔRTT = RTT_sm - RTT_ap
  ΔJitter = Jitter_sm - Jitter_ap

Pure function. No I/O. The diagnostic layer (WU-2.3) attaches the
resulting :class:`SectorDelta` to each :class:`PerSmVerdict` so the
verdict envelope carries the operator-facing numeric evidence.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from nora.probes.metrics import NodeMetrics


class SectorDelta(BaseModel):
    """Per-SM delta vs the AP baseline.

    All four delta fields can be negative (SM better than AP) or
    positive (SM worse than AP). Zero deltas are reported as ``0.0``,
    not as ``None`` — the diagnostic layer reads them without
    optional-handling.
    """

    model_config = ConfigDict(frozen=True)

    luid: str
    """Subscriber LUID; ``""`` for the AP itself (never used in PR2
    but kept as a typed placeholder for symmetry with the per-SM
    verdict)."""

    delta_rtt_avg_ms: float
    """``sm.rtt_avg_ms - ap.rtt_avg_ms``."""

    delta_rtt_p95_ms: float
    """``sm.rtt_p95_ms - ap.rtt_p95_ms``."""

    delta_jitter_avg_ms: float
    """``sm.jitter_avg_ms - ap.jitter_avg_ms``."""

    delta_jitter_max_ms: float
    """``sm.jitter_max_ms - ap.jitter_max_ms``."""


def compute_sector_delta(
    *,
    ap_metrics: NodeMetrics,
    sm_metrics: list[NodeMetrics],
    sm_luids: list[str],
) -> list[SectorDelta]:
    """Compute Δ per SM (in ``sm_luids`` order) against the AP baseline.

    Args:
        ap_metrics: The AP's :class:`NodeMetrics`. The Δ baseline.
        sm_metrics: One :class:`NodeMetrics` per SM, parallel to
            ``sm_luids``.
        sm_luids: LUID strings, parallel to ``sm_metrics``. Pass
            ``""`` for entries that have no LUID (e.g. the AP itself,
            though the diagnostic layer never calls this with the AP
            in the SM slot).

    Returns:
        List of :class:`SectorDelta` in ``sm_luids`` order. Length
        matches ``sm_luids``. An empty input list returns an empty
        list — the caller is expected to handle the empty case before
        invoking (the diagnostic layer guards ``len(sm_metrics) == 0``
        separately so an empty run never reaches here).
    """
    deltas: list[SectorDelta] = []
    for luid, sm in zip(sm_luids, sm_metrics, strict=True):
        deltas.append(
            SectorDelta(
                luid=luid,
                delta_rtt_avg_ms=sm.rtt_avg_ms - ap_metrics.rtt_avg_ms,
                delta_rtt_p95_ms=sm.rtt_p95_ms - ap_metrics.rtt_p95_ms,
                delta_jitter_avg_ms=sm.jitter_avg_ms - ap_metrics.jitter_avg_ms,
                delta_jitter_max_ms=sm.jitter_max_ms - ap_metrics.jitter_max_ms,
            )
        )
    return deltas


__all__ = ["SectorDelta", "compute_sector_delta"]
