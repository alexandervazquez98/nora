"""Inline Markdown summary renderer for a completed probe run (issue #61 / PR3).

Single public function: :func:`render_probe_markdown`. Returns a
~80-120 line Markdown document that the LLM orchestrator can
inline in the chat reply. The Markdown carries:

* A header block (sector, run_id, operator, ISO timestamps,
  duration).
* An executive-verdict card (color-coded emoji + verdict label +
  one-line rationale).
* An AP-metrics bullet list (loss / RTT min/avg/median/p95/max /
  jitter avg/max / drop_burst_max / outage_events).
* A per-SM table (LUID, CINR, Loss%, RTT avg / p95, Jitter avg,
  ΔRTT vs AP, Verdict) + a "ΔRTT vs AP" footer note.
* A footer with the PDF path and ISO timestamp.

Sanitizer contract
==================

Every string that lands in the rendered Markdown MUST be Sanitizer-
cleaned at the call site (PR3 applies :class:`Sanitizer` to every
field before passing it here — see :mod:`nora.probes.sanitize`).
The Markdown body itself is built from pre-cleaned inputs so the
function is intentionally pure: no Sanitizer import, no I/O, no
logging, no time mutation (besides the ``started_at_iso`` /
``finished_at_iso`` formatting).

The function is deterministic — given the same inputs it returns
the same bytes. Tests assert byte-equality on a golden input.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from nora.probes.diagnostic import SectorVerdict
from nora.probes.models import ProbeRunSummary

if TYPE_CHECKING:
    pass

__all__ = ["render_probe_markdown"]


# Verdict → emoji + label. Mirrors the colors the PDF render uses
# (PR3 WU-3.2) so the chat-side and PDF-side visualizations line up.
_VERDICT_BADGE: dict[str, tuple[str, str]] = {
    "EXCELLENT": ("🟢", "EXCELLENT — sector within healthy bounds"),
    "JITTER_MODERATE": ("🟡", "JITTER MODERATE — 5-20 ms jitter, low loss"),
    "JITTER_CRITICAL": ("🟠", "JITTER CRITICAL — > 20 ms jitter"),
    "LOSSY_LINK": ("🟠", "LOSSY LINK — 1-5% packet loss"),
    "UNSTABLE_DROPS": ("🔴", "UNSTABLE DROPS — burst ≥ 8 or loss > 5%"),
    "SECTOR_BACKHAUL_BOTTLENECK": ("🔴", "BACKHAUL BOTTLENECK — AP loss > 1% or RTT > 50 ms"),
    "SECTOR_RF_CONGESTION": ("🔴", "RF CONGESTION — ≥ 60% of SMs show jitter > 5 ms"),
    "ISOLATED_SUBSCRIBER_FAULT": ("🟡", "ISOLATED SUBSCRIBER FAULT — 1 SM degraded"),
}


def render_probe_markdown(
    *,
    verdict: SectorVerdict,
    metrics: ProbeRunSummary,
    run_id: str,
    sector: str,
    device_id: str,
    operator: str,
    started_at_unix: float,
    finished_at_unix: float,
    pdf_path: str | None,
) -> str:
    """Render a one-shot Markdown summary of a completed probe run.

    Args:
        verdict: The :class:`SectorVerdict` from the diagnostic layer.
        metrics: The :class:`ProbeRunSummary` carrying the AP + per-SM
            metrics and the sector-delta list. All numeric fields are
            assumed pre-sanitized (the diagnostic layer never embeds
            IPv4 literals in numeric scalars; the metadata strings
            must already have been passed through the Sanitizer).
        run_id: 8-char hex run identifier. Bypassed by the Sanitizer.
        sector: Operator-friendly sector alias (e.g. ``"norte"``).
        device_id: Inventory device id (e.g. ``"ap-7400-01"``). Pre-sanitized.
        operator: Operator alias (Sanitizer-cleaned at the @mcp.tool boundary).
        started_at_unix: ``time.time()`` at the moment the coordinator
            entered ``run_probe()``.
        finished_at_unix: ``time.time()`` at the moment the verdict was
            assembled (``evaluated_at_unix`` from the diagnostic).
        pdf_path: Absolute path to the rendered PDF (or ``None`` when
            PDF generation failed / is disabled).

    Returns:
        A Markdown string (~120 lines max). Pure function — no I/O,
        no logging, no clock mutation. Deterministic.

    Notes:
        - The function does NOT call the Sanitizer directly. The
          caller (``icmp_list_probe_runs`` / the PDF render path /
          the @mcp.tool boundary) is responsible for pre-cleaning the
          metadata strings; this function trusts its inputs.
        - Verdict / sector names are typed literals from
          ``DiagnosticVerdict``; they never carry private IPv4 literals.
        - Numeric fields are formatted with explicit precision so the
          byte-equal golden test stays stable across Python versions.
    """
    started_iso = _iso(started_at_unix)
    finished_iso = _iso(finished_at_unix)
    duration = max(0.0, finished_at_unix - started_at_unix)
    badge = _VERDICT_BADGE.get(verdict.sector_verdict, ("⚪", verdict.sector_verdict))

    lines: list[str] = []
    lines.append(f"# Probe Run Report — {sector} — {run_id}")
    lines.append("")
    lines.append("## Header")
    lines.append("")
    lines.append(
        f"Operator: `{operator}` | Device: `{device_id}` | Started: `{started_iso}` | "
        f"Duration: `{duration:.0f}s` | Finished: `{finished_iso}`"
    )
    lines.append("")

    # --- Executive verdict -----------------------------------------------------
    lines.append("## Executive Verdict")
    lines.append("")
    lines.append(f"**{badge[0]} {badge[1]}**")
    lines.append("")
    lines.append(f"> {verdict.rationale}")
    lines.append("")
    ap_badge = _VERDICT_BADGE.get(verdict.ap_verdict, ("⚪", verdict.ap_verdict))
    lines.append(f"- AP verdict: {ap_badge[0]} `{verdict.ap_verdict}`")
    if verdict.per_sm:
        lines.append(
            f"- Per-SM verdicts ({len(verdict.per_sm)} SM"
            f"{'s' if len(verdict.per_sm) != 1 else ''}):"
        )
        for entry in verdict.per_sm:
            sm_badge = _VERDICT_BADGE.get(entry.verdict, ("⚪", entry.verdict))
            lines.append(f"  - LUID `{entry.luid}`: {sm_badge[0]} `{entry.verdict}`")
    lines.append("")

    # --- AP metrics ------------------------------------------------------------
    lines.append("## AP metrics")
    lines.append("")
    ap = metrics.ap_metrics
    lines.append(
        f"- Loss: `{ap.packet_loss_pct:.2f}%` "
        f"(`{ap.packets_received}/{ap.packets_transmitted}` received)"
    )
    lines.append(
        f"- RTT min/avg/median/p95/max: "
        f"`{ap.rtt_min_ms:.2f}` / `{ap.rtt_avg_ms:.2f}` / "
        f"`{ap.rtt_median_ms:.2f}` / `{ap.rtt_p95_ms:.2f}` / `{ap.rtt_max_ms:.2f}` ms"
    )
    lines.append(f"- Jitter avg/max: `{ap.jitter_avg_ms:.2f}` / `{ap.jitter_max_ms:.2f}` ms")
    lines.append(f"- Drop burst max: `{ap.drop_burst_max}` consecutive")
    lines.append(f"- Outage events: `{ap.outage_events}` (≥ 3 consecutive losses)")
    lines.append(f"- Samples used: `{ap.samples_used}`")
    lines.append("")

    # --- SMs -------------------------------------------------------------------
    lines.append("## SMs")
    lines.append("")
    if not metrics.sm_metrics:
        lines.append("_No SM data._")
        lines.append("")
    else:
        lines.append("| LUID | Loss% | RTT avg | RTT p95 | Jitter avg | ΔRTT vs AP | Verdict |")
        lines.append("|------|-------|---------|---------|------------|------------|---------|")
        # Build a parallel map verdict.luid → verdict string for the per-SM
        # verdict column. The two lists are parallel by construction (the
        # diagnostic layer preserves order); fall back to "EXCELLENT" when
        # the verdict list is shorter than the metrics list (defensive).
        sm_verdicts_by_luid = {entry.luid: entry.verdict for entry in verdict.per_sm}
        sm_deltas_by_luid = {entry.luid: entry.delta for entry in verdict.per_sm}
        for sm in metrics.sm_metrics:
            luid = sm.target  # PR2 uses LUID string in sm.target.
            sm_verdict = sm_verdicts_by_luid.get(luid, "EXCELLENT")
            delta = sm_deltas_by_luid.get(luid)
            delta_str = f"{delta.delta_rtt_avg_ms:+.2f} ms" if delta is not None else "—"
            lines.append(
                f"| `{luid}` | `{sm.packet_loss_pct:.2f}` | `{sm.rtt_avg_ms:.2f}` | "
                f"`{sm.rtt_p95_ms:.2f}` | `{sm.jitter_avg_ms:.2f}` | "
                f"{delta_str} | `{sm_verdict}` |"
            )
        lines.append("")
        lines.append(
            "ΔRTT is `RTT_sm − RTT_ap`; positive values mean the SM is slower "
            "than the AP baseline (backhaul+RF hop)."
        )
        lines.append("")

    # --- Footer ----------------------------------------------------------------
    lines.append("## Footer")
    lines.append("")
    lines.append(f"- PDF: `{pdf_path or '—'}`")
    lines.append(f"- Generated: `{finished_iso}`")
    lines.append(f"- Verdict evaluated at: `{verdict.evaluated_at_unix:.0f}` (unix)")
    lines.append("")

    return "\n".join(lines)


def _iso(unix_seconds: float) -> str:
    """Format ``unix_seconds`` as an ISO-8601 UTC timestamp with ``+00:00`` suffix.

    Centralised so the Markdown + PDF renderers format timestamps
    identically. ``datetime.fromtimestamp(..., tz=timezone.utc)``
    preserves sub-second precision up to microseconds; tests assert
    the substring ``"T"`` to verify the format.
    """
    dt = datetime.fromtimestamp(unix_seconds, tz=timezone.utc)
    return dt.isoformat()
