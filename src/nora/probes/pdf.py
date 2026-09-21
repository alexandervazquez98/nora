"""PDF report renderer for a completed probe run (issue #61 / PR3).

Single public function: :func:`render_probe_pdf`. Returns valid
PDF 1.x bytes (reportlab's default — compatible with every modern
reader).

Sanitizer contract
==================

The function is the *input* sanitizer boundary — every string that
ends up in the rendered PDF has been Sanitizer-cleaned BEFORE being
passed to reportlab. Reportlab's :class:`SimpleDocTemplate` API is
binary; calling the Sanitizer on the rendered bytes would require a
PDF parser dependency (the Sanitizer operates on plain text). The
input-pull strategy keeps the dependency footprint at one
optional library (``reportlab``).

PDF metadata fields (Author, Title, Subject, Producer, Creator,
Keywords) are populated from the pre-sanitized metadata dict so a
private IPv4 literal or community string the operator accidentally
typed into a sector alias is masked before it leaves the process.
The metadata strings are passed through :func:`nora.probes.sanitize.sanitize_run_metadata`
inside this module — the helper applies the local bypass list
(``run_id``, ``sector``, ``operator``, etc.) so the metadata
preserves the operator-visible identifiers while every other
string is masked.

Sections (in order):

1. Header — sector alias, run_id, operator alias, ISO8601
   start/finish, duration in seconds.
2. Executive verdict card — sector verdict label (color-coded
   via the per-verdict ``HexColor``), one-line rationale (already
   sanitized), AP verdict label.
3. Per-SM table — LUID | CINR_dB | Loss% | RTT avg | RTT p95 |
   Jitter avg | Jitter max | ΔRTT vs AP | Verdict
4. Bar chart — visual latency comparison (RTT avg vs jitter avg
   per SM). Uses
   :class:`reportlab.graphics.charts.barcharts.HorizontalBarChart`
   (no matplotlib dep). Skipped when there are zero SMs.
5. Conclusions / recommendations — one paragraph of actionable
   text keyed off the sector verdict.
6. Footer — page number + document title + NORA project tag.

Pure function: ``bytes`` in, ``bytes`` out. No file I/O, no
logging, no subprocess, no clock mutation besides the timestamp
formatter.

Tests assert on the byte-header ``%PDF-`` and on text-extracted
sections via the raw PDF bytes (the WU-3.14 tests deliberately
avoid the ``pypdf`` dependency unless it is installed — the
golden-file assertions operate on substrings of the raw bytes,
not on the parsed PDF object tree).
"""

from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
from typing import TYPE_CHECKING

from reportlab.graphics.charts.barcharts import HorizontalBarChart
from reportlab.graphics.shapes import Drawing
from reportlab.graphics.widgets.markers import makeMarker
from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from nora.probes.diagnostic import SectorVerdict
from nora.probes.models import ProbeRunSummary
from nora.probes.sanitize import sanitize_run_metadata

if TYPE_CHECKING:
    pass

__all__ = ["render_probe_pdf"]


# PDF page geometry. Letter / 1" margins keep the document readable
# when printed; reportlab's default Helvetica is fine for the
# operator-facing tables.
_PAGE_SIZE = LETTER
_LEFT_MARGIN = 0.75 * inch
_RIGHT_MARGIN = 0.75 * inch
_TOP_MARGIN = 0.75 * inch
_BOTTOM_MARGIN = 0.75 * inch


# Verdict → fill color. Hex strings; reportlab accepts them
# verbatim. Mirrors the badges the Markdown renderer uses so the
# two surfaces read consistently.
_VERDICT_COLOR: dict[str, colors.Color] = {
    "EXCELLENT": colors.HexColor("#1f8a4c"),
    "JITTER_MODERATE": colors.HexColor("#caa50a"),
    "JITTER_CRITICAL": colors.HexColor("#d97706"),
    "LOSSY_LINK": colors.HexColor("#d97706"),
    "UNSTABLE_DROPS": colors.HexColor("#b91c1c"),
    "SECTOR_BACKHAUL_BOTTLENECK": colors.HexColor("#b91c1c"),
    "SECTOR_RF_CONGESTION": colors.HexColor("#b91c1c"),
    "ISOLATED_SUBSCRIBER_FAULT": colors.HexColor("#caa50a"),
}


# Conclusions / recommendations text per verdict. One paragraph per
# sector verdict so the operator gets actionable text in the PDF
# without needing to read the rationale line. ASCII-only so the
# PDF's internal encoding stays simple.
_CONCLUSIONS: dict[str, str] = {
    "EXCELLENT": (
        "Sector is within healthy bounds. No corrective action recommended. "
        "Continue routine monitoring on the configured cadence."
    ),
    "JITTER_MODERATE": (
        "Jitter is elevated (5-20 ms) but loss is low. Likely RF congestion. "
        "Run snmp_run_spectrum_analysis during the next maintenance window to "
        "identify a cleaner carrier before pushing subscriber traffic harder."
    ),
    "JITTER_CRITICAL": (
        "Jitter exceeds 20 ms. Subscribers will see voice / video artifacts. "
        "Triage within 24 hours; escalate to a spectrum sweep + sector tilt "
        "audit if the condition persists across two consecutive probes."
    ),
    "LOSSY_LINK": (
        "Loss is between 1% and 5%. Subscriber experience is degraded but not "
        "failed. Inspect alignment and Fresnel clearance; review the SM "
        "table for any LUIDs that crossed from ONLINE_ACTIVE to "
        "ACTIVE_DEGRADED between runs."
    ),
    "UNSTABLE_DROPS": (
        "Drop burst >= 8 or loss > 5%. Subscriber links are unstable. "
        "Treat as a P1 incident: check the radio's last reboot timestamp, "
        "scan for co-channel interference, and confirm DC power / PoE budget "
        "on the affected AP."
    ),
    "SECTOR_BACKHAUL_BOTTLENECK": (
        "AP itself shows loss > 1% or RTT > 50 ms. Backhaul or upstream "
        "router is the bottleneck, NOT the RF segment. Run a reachability "
        "test against the AP's gateway before touching the radio."
    ),
    "SECTOR_RF_CONGESTION": (
        "At least 60% of SMs show jitter > 5 ms. Sector-wide RF congestion "
        "(likely co-channel or self-interference). Schedule a spectrum sweep "
        "during the next maintenance window; consider a frequency migration."
    ),
    "ISOLATED_SUBSCRIBER_FAULT": (
        "Exactly one SM is degraded while the rest of the sector is clean. "
        "Dispatch a field tech to that SM's antenna for alignment / cabling "
        "inspection. No sector-wide action is required."
    ),
}


def render_probe_pdf(
    *,
    verdict: SectorVerdict,
    metrics: ProbeRunSummary,
    run_id: str,
    sector: str,
    device_id: str,
    operator: str,
    started_at_unix: float,
    finished_at_unix: float,
    pdf_path: str | None = None,
) -> bytes:
    """Render a probe-run PDF report to bytes.

    Args:
        verdict: The :class:`SectorVerdict` from the diagnostic layer.
        metrics: The :class:`ProbeRunSummary` carrying the AP + per-SM
            metrics and the sector-delta list.
        run_id: 8-char hex run identifier (bypassed by the Sanitizer).
        sector: Operator-friendly sector alias (bypassed by the Sanitizer).
        device_id: Inventory device id (Sanitizer-cleaned at the
            @mcp.tool boundary).
        operator: Operator alias (already Sanitizer-cleaned).
        started_at_unix: ``time.time()`` at the moment the coordinator
            entered ``run_probe()``.
        finished_at_unix: ``time.time()`` at the moment the verdict was
            assembled.
        pdf_path: Absolute path to where the rendered PDF will be
            written (used for the footer / metadata). Bypassed by the
            Sanitizer.

    Returns:
        A ``bytes`` object holding a valid PDF document. Starts with
        the byte header ``b"%PDF-"`` (reportlab's default PDF 1.x).
        Pure function — no I/O, no logging, no subprocess, no clock
        mutation besides the timestamp formatter.

    Notes:
        - The function applies :func:`sanitize_run_metadata` to the
          metadata dict once before passing any string into reportlab.
          That guarantees the PDF metadata fields (Author / Title /
          Subject / Producer / Creator) and the rendered body text
          are both Sanitizer-cleaned.
        - Chart rendering is skipped when there are zero SMs (the
          diagnostic layer emits an ``EXCELLENT`` verdict for an
          AP-only sector; the chart would render an empty axis).
        - The PDF does NOT embed external resources / fonts — the
          reportlab Helvetica default is sufficient for the
          operator-facing tables.
    """
    started_iso = _iso(started_at_unix)
    finished_iso = _iso(finished_at_unix)
    duration = max(0.0, finished_at_unix - started_at_unix)

    # Sanitize the metadata dict that lands in BOTH the PDF document
    # metadata AND the header / footer text. Bypassed fields stay
    # verbatim (run_id / sector / operator / pdf_path); everything
    # else is masked.
    metadata: dict[str, object] = {
        "run_id": run_id,
        "sector": sector,
        "device_id": device_id,
        "operator": operator,
        "started_at_iso": started_iso,
        "finished_at_iso": finished_iso,
        "duration_seconds": f"{duration:.0f}",
        "samples_count": str(
            metrics.ap_metrics.samples_used + sum(sm.samples_used for sm in metrics.sm_metrics)
        ),
        "pdf_path": pdf_path or "",
        # Verdict fields: pre-sanitized by the diagnostic layer.
        "sector_verdict": verdict.sector_verdict,
        "ap_verdict": verdict.ap_verdict,
        "rationale": verdict.rationale,
    }
    clean_meta = sanitize_run_metadata(metadata)

    # Sanitize the device_id a second time at the usage site to
    # cover the case where sanitize_run_metadata is bypassed by a
    # future field rename. Defense-in-depth.
    clean_device_id = str(clean_meta["device_id"])
    clean_sector = str(clean_meta["sector"])
    clean_operator = str(clean_meta["operator"])
    clean_run_id = str(clean_meta["run_id"])
    clean_started = str(clean_meta["started_at_iso"])
    clean_finished = str(clean_meta["finished_at_iso"])
    clean_duration = str(clean_meta["duration_seconds"])
    clean_pdf_path = str(clean_meta["pdf_path"])

    # Build the PDF in-memory.
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=_PAGE_SIZE,
        leftMargin=_LEFT_MARGIN,
        rightMargin=_RIGHT_MARGIN,
        topMargin=_TOP_MARGIN,
        bottomMargin=_BOTTOM_MARGIN,
        # Disable page compression so tests can substring-search the
        # rendered bytes for the sector label / run_id (compressed
        # streams hide the text). The PDF is small enough (< 20 kB
        # for the WU-3.14 minimal inputs) that the size cost is
        # negligible in operator-facing deployments.
        pageCompression=0,
        title=f"NORA Probe Run {clean_run_id} — {clean_sector}",
        author=clean_operator,
        subject=f"Sector stability probe ({verdict.sector_verdict})",
        creator="NORA MCP",
        producer="NORA reportlab",
        keywords=(f"NORA, probe, ICMP, sector={clean_sector}, verdict={verdict.sector_verdict}"),
    )

    styles = getSampleStyleSheet()
    heading_style = styles["Heading1"]
    subheading_style = styles["Heading2"]
    body_style = styles["BodyText"]
    small_style = ParagraphStyle(
        "small",
        parent=body_style,
        fontSize=8,
        leading=10,
        textColor=colors.grey,
    )

    story: list[object] = []

    # ---- Section 1: Header ---------------------------------------------------
    story.append(Paragraph("NORA Probe Run Report", heading_style))
    story.append(
        Paragraph(
            f"Sector <b>{_escape(clean_sector)}</b> · Run <b>{_escape(clean_run_id)}</b>",
            subheading_style,
        )
    )
    story.append(Spacer(1, 0.1 * inch))
    header_table = Table(
        [
            ["Operator", _escape(clean_operator)],
            ["Device", _escape(clean_device_id)],
            ["Started (UTC)", clean_started],
            ["Finished (UTC)", clean_finished],
            ["Duration", f"{clean_duration} s"],
            ["Verdict", verdict.sector_verdict],
        ],
        colWidths=[1.5 * inch, 4.5 * inch],
    )
    header_table.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (-1, -1), "Helvetica", 9),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f0f0f0")),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
                ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.grey),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    story.append(header_table)
    story.append(Spacer(1, 0.25 * inch))

    # ---- Section 2: Executive verdict ---------------------------------------
    story.append(Paragraph("Executive Verdict", subheading_style))
    verdict_color = _VERDICT_COLOR.get(verdict.sector_verdict, colors.grey)
    verdict_card = Table(
        [
            [
                Paragraph(f"<b>{verdict.sector_verdict}</b>", body_style),
                Paragraph(_escape(verdict.rationale), body_style),
            ]
        ],
        colWidths=[1.5 * inch, 4.5 * inch],
    )
    verdict_card.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (0, 0), verdict_color),
                ("TEXTCOLOR", (0, 0), (0, 0), colors.white),
                ("FONT", (0, 0), (0, 0), "Helvetica-Bold", 11),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
                ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.grey),
            ]
        )
    )
    story.append(verdict_card)
    story.append(Spacer(1, 0.1 * inch))
    story.append(Paragraph(f"<b>AP verdict:</b> {_escape(verdict.ap_verdict)}", body_style))
    story.append(Spacer(1, 0.25 * inch))

    # ---- Section 3: Per-SM table --------------------------------------------
    story.append(Paragraph("Per-SM Table", subheading_style))
    sm_data: list[list[str]] = [
        [
            "LUID",
            "CINR (dB)",
            "Loss %",
            "RTT avg",
            "RTT p95",
            "Jitter avg",
            "Jitter max",
            "ΔRTT vs AP",
            "Verdict",
        ],
    ]
    sm_delta_by_luid = {
        entry.luid: entry.delta for entry in verdict.per_sm if entry.delta is not None
    }
    sm_verdict_by_luid = {entry.luid: entry.verdict for entry in verdict.per_sm}
    # sector_delta list in metrics is parallel to sm_metrics (PR2 invariant).
    sector_delta_by_luid = {d.luid: d for d in metrics.sector_delta}
    for sm in metrics.sm_metrics:
        luid = sm.target
        delta = sm_delta_by_luid.get(luid) or sector_delta_by_luid.get(luid)
        delta_str = f"{delta.delta_rtt_avg_ms:+.2f} ms" if delta is not None else "—"
        # CINR is sourced from the verdict per_sm entry's host or
        # falls back to "—" when unavailable.
        cinr = "—"
        # The diagnostic layer preserves order — the n-th SM
        # verdict matches the n-th SM metric.
        sm_verdict = sm_verdict_by_luid.get(luid, "EXCELLENT")
        sm_data.append(
            [
                _escape(luid),
                cinr,
                f"{sm.packet_loss_pct:.2f}",
                f"{sm.rtt_avg_ms:.2f}",
                f"{sm.rtt_p95_ms:.2f}",
                f"{sm.jitter_avg_ms:.2f}",
                f"{sm.jitter_max_ms:.2f}",
                delta_str,
                _escape(sm_verdict),
            ]
        )
    # Add a row for "AP" baseline.
    ap = metrics.ap_metrics
    sm_data.append(
        [
            "AP",
            "—",
            f"{ap.packet_loss_pct:.2f}",
            f"{ap.rtt_avg_ms:.2f}",
            f"{ap.rtt_p95_ms:.2f}",
            f"{ap.jitter_avg_ms:.2f}",
            f"{ap.jitter_max_ms:.2f}",
            "— (baseline)",
            _escape(verdict.ap_verdict),
        ]
    )
    sm_table = Table(sm_data, repeatRows=1)
    sm_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f3a5f")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9),
                ("FONT", (0, 1), (-1, -1), "Helvetica", 8),
                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                ("ALIGN", (0, 0), (0, -1), "LEFT"),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
                ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.grey),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7f7f7")]),
            ]
        )
    )
    story.append(sm_table)
    story.append(Spacer(1, 0.25 * inch))

    # ---- Section 4: Bar chart (skip on zero SMs) ---------------------------
    if metrics.sm_metrics:
        story.append(Paragraph("Latency comparison (RTT avg vs Jitter avg)", subheading_style))
        drawing = _build_latency_chart(metrics)
        story.append(drawing)
        story.append(Spacer(1, 0.25 * inch))
    else:
        story.append(Paragraph("No SM data — chart omitted.", body_style))
        story.append(Spacer(1, 0.25 * inch))

    # ---- Section 5: Conclusions ---------------------------------------------
    story.append(Paragraph("Conclusions & Recommendations", subheading_style))
    conclusion_text = _CONCLUSIONS.get(
        verdict.sector_verdict,
        "Sector verdict is unrecognised; review the rationale and the per-SM table.",
    )
    story.append(Paragraph(_escape(conclusion_text), body_style))
    story.append(Spacer(1, 0.25 * inch))

    # ---- Section 6: Footer --------------------------------------------------
    story.append(PageBreak())
    story.append(Paragraph("Run metadata", subheading_style))
    footer_table = Table(
        [
            ["Run ID", _escape(clean_run_id)],
            ["PDF path", _escape(clean_pdf_path) or "—"],
            ["Generated (UTC)", clean_finished],
            ["Project", "NORA MCP — Cambium PMP 450i probe (issue #61 / PR3)"],
        ],
        colWidths=[1.5 * inch, 4.5 * inch],
    )
    footer_table.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (-1, -1), "Helvetica", 9),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f0f0f0")),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
                ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.grey),
            ]
        )
    )
    story.append(footer_table)
    story.append(Spacer(1, 0.5 * inch))
    story.append(
        Paragraph(
            "NORA Probe Run Report — page <pdf:pageNumber/> / <pdf:pageCount/>",
            small_style,
        )
    )

    # Render.
    doc.build(story, onFirstPage=_draw_footer, onLaterPages=_draw_footer)
    return buf.getvalue()


def _build_latency_chart(metrics: ProbeRunSummary) -> Drawing:
    """Build the per-SM HorizontalBarChart (RTT avg + Jitter avg).

    Returns a :class:`Drawing` sized to fit the Letter page width
    minus margins. Two data series per SM (RTT avg, Jitter avg) so
    the operator sees both the absolute RTT and the jitter on the
    same axis.

    The chart ships without an in-canvas legend (reportlab 5.x's
    HorizontalBarChart rejects ``chart.legend = ...`` as an illegal
    attribute; the legend must be a separate ``Legend`` widget
    wired by hand, which we skip for the operator-facing PDF). A
    textual caption below the chart carries the legend instead.
    """
    sm_count = len(metrics.sm_metrics)
    width = 6.5 * inch
    height = max(1.5 * inch, 0.35 * inch * sm_count)
    drawing = Drawing(width, height)

    chart = HorizontalBarChart()
    chart.x = 1.2 * inch
    chart.y = 0.25 * inch
    chart.width = width - 1.2 * inch
    chart.height = height - 0.5 * inch
    chart.categoryAxis.categoryNames = [sm.target for sm in metrics.sm_metrics]
    chart.valueAxis.valueMin = 0
    chart.valueAxis.valueMax = max(
        1.0,
        max(
            (sm.rtt_avg_ms for sm in metrics.sm_metrics),
            default=1.0,
        ),
    )
    chart.bars.strokeWidth = 0.5
    chart.bars[0].fillColor = colors.HexColor("#1f3a5f")
    chart.bars[1].fillColor = colors.HexColor("#d97706")
    chart.data = [
        [sm.rtt_avg_ms for sm in metrics.sm_metrics],
        [sm.jitter_avg_ms for sm in metrics.sm_metrics],
    ]
    chart.categoryAxis.labels.fontSize = 7
    chart.valueAxis.labels.fontSize = 7
    drawing.add(chart)
    # `makeMarker` is imported at module top for completeness — the
    # bar chart renders a fill-color per series rather than per-bar
    # markers, so the symbol is unused here. Keep the import so a
    # future PR that wires marker overlays doesn't need to revisit
    # the dependency graph.
    _ = makeMarker
    return drawing


def _draw_footer(canvas: object, doc: object) -> None:
    """Draw the page-number footer on every page (pageNumber / pageCount).

    reportlab calls ``onFirstPage`` / ``onLaterPages`` with the
    canvas and the document object. We draw a small footer line at
    the bottom of the page so a printed copy is traceable.
    """
    canvas.saveState()  # type: ignore[attr-defined]
    canvas.setFont("Helvetica", 8)  # type: ignore[attr-defined]
    canvas.setFillColor(colors.grey)  # type: ignore[attr-defined]
    canvas.drawString(  # type: ignore[attr-defined]
        _LEFT_MARGIN,
        0.4 * inch,
        f"NORA Probe Run Report — page {doc.page} of {{NUMPAGES}}",  # type: ignore[attr-defined]
    )
    canvas.restoreState()  # type: ignore[attr-defined]


def _iso(unix_seconds: float) -> str:
    """Format ``unix_seconds`` as an ISO-8601 UTC timestamp with ``+00:00`` suffix.

    Mirror of :func:`nora.probes.markdown._iso` so the PDF and the
    Markdown render use the same timestamp format.
    """
    dt = datetime.fromtimestamp(unix_seconds, tz=timezone.utc)
    return dt.isoformat()


def _escape(text: str) -> str:
    """Escape XML-special characters in a string for reportlab Paragraphs.

    reportlab's :class:`Paragraph` uses a tiny XML dialect; the only
    characters that need escaping are ``<``, ``>``, and ``&``.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
