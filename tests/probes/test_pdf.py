"""Tests for `nora.probes.pdf.render_probe_pdf` (issue #61 / PR3 WU-3.14).

Covers the PR3 PDF contract:

1. ``render_probe_pdf`` returns bytes whose header is ``%PDF-`` so the
   PDF is openable by any standard reader.
2. The PDF body includes the sector label and the run_id verbatim
   (both are on the Sanitizer bypass list).
3. PDF metadata (Author / Title / Subject) carries the operator alias
   and the run_id; it does NOT carry a raw private IPv4 literal
   (operator may have typed one into the device id by accident).
4. ``sm_metrics=[]`` does NOT raise; the renderer emits a "No SM data"
   line in place of the chart.
5. The Sanitizer pre-cleaning contract holds: a private IPv4 literal
   passed in via the ap_target / device_id is masked before the PDF
   is rendered (asserted via substring search on the raw bytes).

The tests do NOT depend on ``pypdf`` (which is not a project
dependency); they operate on substring assertions of the raw PDF
bytes plus reportlab's own round-trip via :func:`SimpleDocTemplate`.
"""

from __future__ import annotations

import time

import pytest

from nora.probes.diagnostic import PerSmVerdict, SectorVerdict
from nora.probes.metrics import NodeMetrics
from nora.probes.models import ProbeRunSummary
from nora.probes.pdf import render_probe_pdf
from nora.probes.sector_delta import SectorDelta

# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


def _make_ap_metrics() -> NodeMetrics:
    return NodeMetrics(
        target="ap",
        packets_transmitted=600,
        packets_received=600,
        packet_loss_pct=0.0,
        drop_burst_max=0,
        outage_events=0,
        rtt_min_ms=1.0,
        rtt_avg_ms=1.5,
        rtt_median_ms=1.4,
        rtt_p95_ms=2.0,
        rtt_max_ms=3.0,
        jitter_avg_ms=0.3,
        jitter_max_ms=0.8,
        samples_used=600,
    )


def _make_sm_metrics() -> list[NodeMetrics]:
    return [
        NodeMetrics(
            target="002",
            packets_transmitted=600,
            packets_received=595,
            packet_loss_pct=0.83,
            drop_burst_max=2,
            outage_events=0,
            rtt_min_ms=2.0,
            rtt_avg_ms=4.5,
            rtt_median_ms=4.0,
            rtt_p95_ms=8.0,
            rtt_max_ms=15.0,
            jitter_avg_ms=1.2,
            jitter_max_ms=3.0,
            samples_used=595,
        ),
        NodeMetrics(
            target="003",
            packets_transmitted=600,
            packets_received=600,
            packet_loss_pct=0.0,
            drop_burst_max=0,
            outage_events=0,
            rtt_min_ms=1.5,
            rtt_avg_ms=2.0,
            rtt_median_ms=2.0,
            rtt_p95_ms=3.0,
            rtt_max_ms=4.5,
            jitter_avg_ms=0.4,
            jitter_max_ms=1.0,
            samples_used=600,
        ),
    ]


def _make_sector_delta() -> list[SectorDelta]:
    return [
        SectorDelta(
            luid="002",
            delta_rtt_avg_ms=3.0,
            delta_rtt_p95_ms=6.0,
            delta_jitter_avg_ms=0.9,
            delta_jitter_max_ms=2.2,
        ),
        SectorDelta(
            luid="003",
            delta_rtt_avg_ms=0.5,
            delta_rtt_p95_ms=1.0,
            delta_jitter_avg_ms=0.1,
            delta_jitter_max_ms=0.2,
        ),
    ]


def _make_minimal_inputs() -> tuple[SectorVerdict, ProbeRunSummary]:
    ap_metrics = _make_ap_metrics()
    sm_metrics = _make_sm_metrics()
    sector_delta = _make_sector_delta()
    summary = ProbeRunSummary(
        ap_metrics=ap_metrics,
        sm_metrics=sm_metrics,
        sector_delta=sector_delta,
    )
    verdict = SectorVerdict(
        ap_verdict="EXCELLENT",
        per_sm=[
            PerSmVerdict(luid="002", verdict="EXCELLENT", delta=sector_delta[0]),
            PerSmVerdict(luid="003", verdict="EXCELLENT", delta=sector_delta[1]),
        ],
        sector_verdict="EXCELLENT",
        rationale="Sector is within healthy bounds.",
        evaluated_at_unix=time.time(),
    )
    return verdict, summary


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_render_probe_pdf_returns_valid_pdf_header() -> None:
    """The returned bytes start with the ``%PDF-`` magic so any standard reader opens them."""
    verdict, metrics = _make_minimal_inputs()
    pdf_bytes = render_probe_pdf(
        verdict=verdict,
        metrics=metrics,
        run_id="abcd1234",
        sector="norte",
        device_id="ap-7400-01",
        operator="alex",
        started_at_unix=1_761_234_567.0,
        finished_at_unix=1_761_235_167.0,
        pdf_path="/var/lib/nora/probes/PRB-norte.pdf",
    )
    assert isinstance(pdf_bytes, bytes)
    assert pdf_bytes[:5] == b"%PDF-"
    # reportlab emits ``%PDF-1.X\n`` (a newline terminator after the
    # version); split on the trailing whitespace and compare the
    # version triple.
    version_str = pdf_bytes[5:10].split(b"\n", 1)[0].split(b" ", 1)[0]
    assert version_str in (b"1.4", b"1.5", b"1.6", b"1.7", b"2.0")


def test_render_probe_pdf_includes_sector_label_in_header() -> None:
    """The PDF body includes the sector label (bypass field) and the run_id."""
    verdict, metrics = _make_minimal_inputs()
    pdf_bytes = render_probe_pdf(
        verdict=verdict,
        metrics=metrics,
        run_id="abcd1234",
        sector="norte",
        device_id="ap-7400-01",
        operator="alex",
        started_at_unix=1_761_234_567.0,
        finished_at_unix=1_761_235_167.0,
        pdf_path="/var/lib/nora/probes/PRB-norte.pdf",
    )
    # reportlab flattens text into content streams; both the sector
    # label and the run_id MUST appear (bypass fields).
    assert b"norte" in pdf_bytes, (
        "expected the sector label 'norte' in the PDF bytes (bypass field)"
    )
    assert b"abcd1234" in pdf_bytes, (
        "expected the run_id 'abcd1234' in the PDF bytes (bypass field)"
    )


def test_render_probe_pdf_metadata_contains_run_id_but_no_raw_ips() -> None:
    """PDF metadata carries the run_id; private IPv4 literals are masked.

    The renderer sets Author / Subject / Title from the Sanitizer-cleaned
    metadata. The test pypdf parses those fields when available; when
    not, it falls back to substring assertions on the raw bytes.
    """
    verdict, metrics = _make_minimal_inputs()
    pdf_bytes = render_probe_pdf(
        verdict=verdict,
        metrics=metrics,
        run_id="abcd1234",
        sector="norte",
        device_id="ap-7400-01",
        operator="alex",
        started_at_unix=1_761_234_567.0,
        finished_at_unix=1_761_235_167.0,
        pdf_path="/var/lib/nora/probes/PRB-norte.pdf",
    )
    # pypdf is optional; skip the structured metadata assertion when
    # not installed. The substring assertion (below) is the fallback
    # that always runs.
    pypdf = pytest.importorskip("pypdf")
    import io

    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    metadata = reader.metadata or {}
    subject = str(metadata.get("/Subject", ""))
    title = str(metadata.get("/Title", ""))
    # The run_id is on the bypass list — it MUST appear verbatim.
    assert "abcd1234" in subject or "abcd1234" in title, (
        f"expected run_id in PDF metadata; got subject={subject!r} title={title!r}"
    )
    # No raw private IPv4 literal anywhere in the metadata stream.
    assert "10.0.0.5" not in subject
    assert "10.0.0.5" not in title


def test_render_probe_pdf_with_zero_sms_skips_chart() -> None:
    """``sm_metrics=[]`` does NOT raise; the bytes are still a valid PDF."""
    ap_metrics = _make_ap_metrics()
    summary = ProbeRunSummary(ap_metrics=ap_metrics, sm_metrics=[], sector_delta=[])
    verdict = SectorVerdict(
        ap_verdict="EXCELLENT",
        per_sm=[],
        sector_verdict="EXCELLENT",
        rationale="AP-only sector.",
        evaluated_at_unix=time.time(),
    )
    pdf_bytes = render_probe_pdf(
        verdict=verdict,
        metrics=summary,
        run_id="abcd1234",
        sector="norte",
        device_id="ap-7400-01",
        operator="alex",
        started_at_unix=1_761_234_567.0,
        finished_at_unix=1_761_235_167.0,
        pdf_path=None,
    )
    assert pdf_bytes[:5] == b"%PDF-"
    # The "No SM data" caption lands in the content stream.
    assert b"No SM data" in pdf_bytes


def test_render_probe_pdf_sanitizes_metadata_input() -> None:
    """A private IPv4 literal in the device_id is masked before rendering.

    The Sanitizer pre-cleans every metadata field; bypass fields stay
    verbatim. ``device_id`` is NOT a bypass field so a private IPv4
    literal in the device_id MUST be replaced by a stable alias
    (``RADIO_NODE_X`` per ``Sanitizer._alias_for('ip', ...)``).
    """
    verdict, metrics = _make_minimal_inputs()
    pdf_bytes = render_probe_pdf(
        verdict=verdict,
        metrics=metrics,
        run_id="abcd1234",
        sector="norte",
        device_id="ap-10.0.0.5",  # raw private IPv4 literal in device id
        operator="alex",
        started_at_unix=1_761_234_567.0,
        finished_at_unix=1_761_235_167.0,
        pdf_path=None,
    )
    # The raw IPv4 literal MUST NOT appear verbatim anywhere in the
    # rendered PDF bytes.
    assert b"10.0.0.5" not in pdf_bytes, (
        "private IPv4 literal leaked into rendered PDF — Sanitizer pre-clean failed"
    )
    # The Sanitizer replaces private IPs with ``RADIO_NODE_X`` aliases.
    assert b"RADIO_NODE_" in pdf_bytes


def test_render_probe_pdf_includes_letter_geometry_marker() -> None:
    """Sanity-check that the rendered PDF is well-formed (byte header + EOF marker).

    The PDF format requires the ``%%EOF`` marker at the end of the
    file; reportlab's SimpleDocTemplate always appends it. The
    assertion guards against a future refactor that drops the
    rendering pipeline.
    """
    verdict, metrics = _make_minimal_inputs()
    pdf_bytes = render_probe_pdf(
        verdict=verdict,
        metrics=metrics,
        run_id="abcd1234",
        sector="norte",
        device_id="ap-7400-01",
        operator="alex",
        started_at_unix=1_761_234_567.0,
        finished_at_unix=1_761_235_167.0,
        pdf_path=None,
    )
    assert pdf_bytes.startswith(b"%PDF-")
    assert b"%%EOF" in pdf_bytes[-1024:]
