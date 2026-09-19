"""Tests for the post-sweep spectrum XML HTTP fetch + RF bin decoding — issue #70 WU-1.

Hermetic test suite for ``nora.drivers.snmp_pmp450i.spectrum_http``:

* ``SpectrumBin`` model — frozen, JSON-serialisable, polarization validation.
* ``build_spectrum_url`` — default (HTTP/80/SpectrumAnalysis.xml) + overrides.
* ``fetch_spectrum_xml`` — happy path, retry loop, retry exhaustion, non-retryable
  4xx, timeout, ``max_retries=0`` bypass. Uses ``httpx.MockTransport`` for full
  hermeticity (no real network).
* ``parse_spectrum_xml`` — happy path on the fixture, mixed V/H, empty root,
  malformed XML, missing attributes, unexpected root.
* ``rank_clean_frequencies`` — worst-case min first ordering, ties broken by
  frequency ascending, top_n cap, empty input.
* ``noise_floor_per_channel`` — worst-leg avg per channel, empty input.
* Exception contract — both new errors inherit ``DriverError``.

Zero-Leakage: only TEST-NET-1 (``192.0.2.x``) host literals; no real IPs.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from nora.drivers.exceptions import (
    DriverError,
    SpectrumHttpFetchError,
    SpectrumXmlParseError,
)
from nora.drivers.snmp_pmp450i.spectrum_http import (
    SpectrumBin,
    build_spectrum_url,
    fetch_spectrum_xml,
    noise_floor_per_channel,
    parse_spectrum_xml,
    rank_clean_frequencies,
)

# ---------------------------------------------------------------------------
# Fixture loader + hermetic mock-client helper
# ---------------------------------------------------------------------------


FIXTURE_PATH = (
    Path(__file__).parent / "data" / "fixtures" / "spectrum" / "pmp450i_spectrum_sample.xml"
)


@pytest.fixture
def spectrum_xml_text() -> str:
    """Load the sanitized Cambium spectrum XML fixture."""
    return FIXTURE_PATH.read_text(encoding="utf-8")


def _make_mock_client(handler) -> httpx.Client:
    """Build a one-shot httpx.Client wired to ``httpx.MockTransport``."""
    return httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0)


# ---------------------------------------------------------------------------
# SpectrumBin — frozen model contract
# ---------------------------------------------------------------------------


def test_spectrum_bin_is_frozen_and_serialisable() -> None:
    """``SpectrumBin`` is frozen (assignment raises) and JSON-round-trips."""
    bin_ = SpectrumBin(frequency_mhz=3500.0, polarization="V", avg_dbm=-68, max_dbm=-68)
    with pytest.raises(ValidationError):
        bin_.polarization = "H"  # type: ignore[misc]
    payload = bin_.model_dump(mode="json")
    assert payload == {
        "frequency_mhz": 3500.0,
        "polarization": "V",
        "avg_dbm": -68,
        "max_dbm": -68,
    }


def test_spectrum_bin_rejects_unknown_polarization() -> None:
    """``SpectrumBin`` rejects polarizations outside the ``Literal["V","H"]`` set."""
    with pytest.raises(ValidationError):
        SpectrumBin(frequency_mhz=3500.0, polarization="X", avg_dbm=-68, max_dbm=-68)


# ---------------------------------------------------------------------------
# build_spectrum_url — defaults + overrides + empty-host guard
# ---------------------------------------------------------------------------


def test_build_spectrum_url_default() -> None:
    """Default URL is HTTP on port 80 with the canonical ``/SpectrumAnalysis.xml`` path."""
    assert build_spectrum_url("192.0.2.10") == "http://192.0.2.10:80/SpectrumAnalysis.xml"


def test_build_spectrum_url_custom_scheme_port_path() -> None:
    """All three URL components (scheme / port / path) are overridable."""
    assert (
        build_spectrum_url("192.0.2.10", scheme="https", port=8443, path="/spectrum.xml")
        == "https://192.0.2.10:8443/spectrum.xml"
    )


def test_build_spectrum_url_rejects_empty_host() -> None:
    """An empty host raises ``ValueError`` — no wire frame is emitted."""
    with pytest.raises(ValueError):
        build_spectrum_url("")


# ---------------------------------------------------------------------------
# fetch_spectrum_xml — httpx.MockTransport hermetic harness
# ---------------------------------------------------------------------------


def test_fetch_spectrum_xml_happy_path_one_attempt() -> None:
    """Single 200 response → body returned verbatim; ``max_retries=0`` still attempts once."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text="<Spectrum_Analyzer/>")

    client = _make_mock_client(handler)
    try:
        body = fetch_spectrum_xml(
            "192.0.2.10", http_client=client, max_retries=0, retry_delay_seconds=0.0
        )
    finally:
        client.close()
    assert body == "<Spectrum_Analyzer/>"
    assert len(seen) == 1


def test_fetch_spectrum_xml_retries_on_connect_error_then_succeeds() -> None:
    """Four ``ConnectError`` attempts then a 200 → succeeds on the 5th try."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 4:
            raise httpx.ConnectError("refused")
        return httpx.Response(200, text="<Spectrum_Analyzer/>")

    client = _make_mock_client(handler)
    try:
        body = fetch_spectrum_xml(
            "192.0.2.10",
            http_client=client,
            max_retries=5,
            retry_delay_seconds=0.0,
        )
    finally:
        client.close()
    assert body == "<Spectrum_Analyzer/>"
    assert calls["n"] == 5


def test_fetch_spectrum_xml_exhausted_retries_raises_typed_error() -> None:
    """All attempts raise ``ConnectError`` → ``SpectrumHttpFetchError(attempts=3, status=0)``."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client = _make_mock_client(handler)
    try:
        with pytest.raises(SpectrumHttpFetchError) as exc_info:
            fetch_spectrum_xml(
                "192.0.2.10",
                http_client=client,
                max_retries=2,
                retry_delay_seconds=0.0,
            )
    finally:
        client.close()
    err = exc_info.value
    assert err.host == "192.0.2.10"
    assert err.status_code == 0
    assert err.attempts == 3


def test_fetch_spectrum_xml_non_retryable_4xx_raises_immediately() -> None:
    """404 is fatal — one attempt only; no retries on non-retryable status."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, text="not found")

    client = _make_mock_client(handler)
    try:
        with pytest.raises(SpectrumHttpFetchError) as exc_info:
            fetch_spectrum_xml(
                "192.0.2.10",
                http_client=client,
                max_retries=5,
                retry_delay_seconds=0.0,
            )
    finally:
        client.close()
    err = exc_info.value
    assert err.status_code == 404
    assert err.attempts == 1
    assert calls["n"] == 1


def test_fetch_spectrum_xml_retryable_5xx_eventually_succeeds() -> None:
    """503 is retryable — two 503s then 200 → succeeds on the 3rd attempt."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, text="<Spectrum_Analyzer/>")

    client = _make_mock_client(handler)
    try:
        body = fetch_spectrum_xml(
            "192.0.2.10",
            http_client=client,
            max_retries=5,
            retry_delay_seconds=0.0,
        )
    finally:
        client.close()
    assert body == "<Spectrum_Analyzer/>"
    assert calls["n"] == 3


def test_fetch_spectrum_xml_timeout_is_transient() -> None:
    """``TimeoutException`` counts as transient — two timeouts then 200 → succeeds."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise httpx.TimeoutException("slow")
        return httpx.Response(200, text="<Spectrum_Analyzer/>")

    client = _make_mock_client(handler)
    try:
        body = fetch_spectrum_xml(
            "192.0.2.10",
            http_client=client,
            max_retries=5,
            retry_delay_seconds=0.0,
        )
    finally:
        client.close()
    assert body == "<Spectrum_Analyzer/>"
    assert calls["n"] == 3


# ---------------------------------------------------------------------------
# parse_spectrum_xml — schema contract
# ---------------------------------------------------------------------------


def test_parse_spectrum_xml_happy_path_returns_32_bins(spectrum_xml_text: str) -> None:
    """The fixture parses to exactly 32 ``SpectrumBin`` records across 8 unique frequencies."""
    bins = parse_spectrum_xml(spectrum_xml_text)
    assert len(bins) == 32
    frequencies = sorted({b.frequency_mhz for b in bins})
    assert frequencies == [3500.0, 3540.0, 3550.0, 3560.0, 3600.0, 3620.0, 3650.0, 3700.0]
    # Sanity: all 32 are SpectrumBin instances.
    assert all(isinstance(b, SpectrumBin) for b in bins)


def test_parse_spectrum_xml_mixed_polarizations(spectrum_xml_text: str) -> None:
    """The fixture has 16 V bins and 16 H bins (one of each per frequency row)."""
    bins = parse_spectrum_xml(spectrum_xml_text)
    v_count = sum(1 for b in bins if b.polarization == "V")
    h_count = sum(1 for b in bins if b.polarization == "H")
    assert v_count == 16
    assert h_count == 16


def test_parse_spectrum_xml_empty_root_returns_empty_list() -> None:
    """``<Spectrum_Analyzer></Spectrum_Analyzer>`` is valid and returns ``[]``."""
    assert parse_spectrum_xml("<Spectrum_Analyzer></Spectrum_Analyzer>") == []


def test_parse_spectrum_xml_empty_payload_raises() -> None:
    """Empty / whitespace-only payload → ``SpectrumXmlParseError``."""
    with pytest.raises(SpectrumXmlParseError):
        parse_spectrum_xml("")
    with pytest.raises(SpectrumXmlParseError):
        parse_spectrum_xml("   \n\t  ")


def test_parse_spectrum_xml_malformed_xml_raises() -> None:
    """Non-XML payload → ``SpectrumXmlParseError``."""
    with pytest.raises(SpectrumXmlParseError):
        parse_spectrum_xml("not-xml")


def test_parse_spectrum_xml_missing_freq_avg_attribute_raises() -> None:
    """A ``<Freq>`` missing the ``avg`` attribute → ``SpectrumXmlParseError``."""
    with pytest.raises(SpectrumXmlParseError):
        parse_spectrum_xml(
            '<Spectrum_Analyzer><Freq f="3500.0 V" max="-68" /></Spectrum_Analyzer>'
        )


def test_parse_spectrum_xml_unexpected_root_raises() -> None:
    """A root other than ``<Spectrum_Analyzer>`` → ``SpectrumXmlParseError``."""
    with pytest.raises(SpectrumXmlParseError):
        parse_spectrum_xml("<Other/>")


def test_parse_spectrum_xml_unknown_polarization_raises() -> None:
    """An unknown polarization token → ``SpectrumXmlParseError``."""
    with pytest.raises(SpectrumXmlParseError):
        parse_spectrum_xml(
            '<Spectrum_Analyzer><Freq f="3500.0 X" avg="-68" max="-68" /></Spectrum_Analyzer>'
        )


# ---------------------------------------------------------------------------
# rank_clean_frequencies — worst-case min first ordering
# ---------------------------------------------------------------------------


def test_rank_clean_frequencies_empty_returns_empty() -> None:
    """Empty input → empty list."""
    assert rank_clean_frequencies([]) == []


def test_rank_clean_frequencies_orders_ascending_by_worst_leg() -> None:
    """Worst-leg avg per channel is the sort key; cleanest first."""
    bins = [
        SpectrumBin(frequency_mhz=3650.0, polarization="V", avg_dbm=-60, max_dbm=-59),  # worst
        SpectrumBin(frequency_mhz=3500.0, polarization="V", avg_dbm=-68, max_dbm=-68),
        SpectrumBin(frequency_mhz=3560.0, polarization="V", avg_dbm=-82, max_dbm=-81),  # cleanest
        SpectrumBin(frequency_mhz=3560.0, polarization="H", avg_dbm=-91, max_dbm=-90),
        SpectrumBin(frequency_mhz=3500.0, polarization="H", avg_dbm=-65, max_dbm=-64),
    ]
    assert rank_clean_frequencies(bins, top_n=10) == [3560.0, 3500.0, 3650.0]


def test_rank_clean_frequencies_top_n_cap() -> None:
    """``top_n`` caps the returned list length."""
    bins = [
        SpectrumBin(frequency_mhz=3650.0, polarization="V", avg_dbm=-60, max_dbm=-59),
        SpectrumBin(frequency_mhz=3500.0, polarization="V", avg_dbm=-68, max_dbm=-68),
        SpectrumBin(frequency_mhz=3560.0, polarization="V", avg_dbm=-82, max_dbm=-81),
        SpectrumBin(frequency_mhz=3560.0, polarization="H", avg_dbm=-91, max_dbm=-90),
        SpectrumBin(frequency_mhz=3500.0, polarization="H", avg_dbm=-65, max_dbm=-64),
    ]
    assert rank_clean_frequencies(bins, top_n=2) == [3560.0, 3500.0]


def test_rank_clean_frequencies_ties_broken_by_frequency_ascending() -> None:
    """Ties on worst-leg avg → lower frequency wins (ascending)."""
    bins = [
        SpectrumBin(frequency_mhz=3700.0, polarization="V", avg_dbm=-70, max_dbm=-69),
        SpectrumBin(frequency_mhz=3600.0, polarization="V", avg_dbm=-70, max_dbm=-69),
    ]
    assert rank_clean_frequencies(bins, top_n=10) == [3600.0, 3700.0]


# ---------------------------------------------------------------------------
# noise_floor_per_channel — worst-leg avg per frequency
# ---------------------------------------------------------------------------


def test_noise_floor_per_channel_empty_returns_empty() -> None:
    """Empty input → empty dict."""
    assert noise_floor_per_channel([]) == {}


def test_noise_floor_per_channel_worst_leg_per_frequency() -> None:
    """Per frequency, take the max avg_dbm across all bins (worst leg)."""
    bins = [
        SpectrumBin(frequency_mhz=3650.0, polarization="V", avg_dbm=-60, max_dbm=-59),
        SpectrumBin(frequency_mhz=3500.0, polarization="V", avg_dbm=-68, max_dbm=-68),
        SpectrumBin(frequency_mhz=3560.0, polarization="V", avg_dbm=-82, max_dbm=-81),
        SpectrumBin(frequency_mhz=3560.0, polarization="H", avg_dbm=-91, max_dbm=-90),
        SpectrumBin(frequency_mhz=3500.0, polarization="H", avg_dbm=-65, max_dbm=-64),
    ]
    assert noise_floor_per_channel(bins) == {
        "3500.0": -65.0,
        "3560.0": -82.0,
        "3650.0": -60.0,
    }


# ---------------------------------------------------------------------------
# Exception contract — both new errors inherit DriverError
# ---------------------------------------------------------------------------


def test_spectrum_http_errors_inherit_driver_error() -> None:
    """``SpectrumHttpFetchError`` and ``SpectrumXmlParseError`` are ``DriverError`` subclasses."""
    http_err = SpectrumHttpFetchError(host="x", status_code=500, attempts=1, message="m")
    parse_err = SpectrumXmlParseError(message="m", line=0)
    assert isinstance(http_err, DriverError)
    assert isinstance(parse_err, DriverError)
    # Field projection is preserved on the http_err instance.
    assert http_err.host == "x"
    assert http_err.status_code == 500
    assert http_err.attempts == 1
    assert parse_err.message == "m"
    assert parse_err.line == 0


__all__ = [
    "test_spectrum_bin_is_frozen_and_serialisable",
    "test_spectrum_bin_rejects_unknown_polarization",
    "test_build_spectrum_url_default",
    "test_build_spectrum_url_custom_scheme_port_path",
    "test_build_spectrum_url_rejects_empty_host",
    "test_fetch_spectrum_xml_happy_path_one_attempt",
    "test_fetch_spectrum_xml_retries_on_connect_error_then_succeeds",
    "test_fetch_spectrum_xml_exhausted_retries_raises_typed_error",
    "test_fetch_spectrum_xml_non_retryable_4xx_raises_immediately",
    "test_fetch_spectrum_xml_retryable_5xx_eventually_succeeds",
    "test_fetch_spectrum_xml_timeout_is_transient",
    "test_parse_spectrum_xml_happy_path_returns_32_bins",
    "test_parse_spectrum_xml_mixed_polarizations",
    "test_parse_spectrum_xml_empty_root_returns_empty_list",
    "test_parse_spectrum_xml_empty_payload_raises",
    "test_parse_spectrum_xml_malformed_xml_raises",
    "test_parse_spectrum_xml_missing_freq_avg_attribute_raises",
    "test_parse_spectrum_xml_unexpected_root_raises",
    "test_parse_spectrum_xml_unknown_polarization_raises",
    "test_rank_clean_frequencies_empty_returns_empty",
    "test_rank_clean_frequencies_orders_ascending_by_worst_leg",
    "test_rank_clean_frequencies_top_n_cap",
    "test_rank_clean_frequencies_ties_broken_by_frequency_ascending",
    "test_noise_floor_per_channel_empty_returns_empty",
    "test_noise_floor_per_channel_worst_leg_per_frequency",
    "test_spectrum_http_errors_inherit_driver_error",
]