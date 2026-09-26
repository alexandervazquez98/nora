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
from nora.drivers.oid_catalog import OidCatalog
from nora.drivers.snmp_pmp450i.spectrum import _read_band_range_after_sweep
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
# Namespaced variant — mirrors the operator-observed `SpectrumAnalysis.xml`
# emitted by real Cambium PMP 450i hardware (firmware 25.1) with the default
# `xmlns="http://www.cambiumnetworks.com/spectrum"` declaration. Issue #78.
NAMESPACED_FIXTURE_PATH = (
    Path(__file__).parent / "data" / "fixtures" / "spectrum" / "pmp450i_spectrum_namespaced.xml"
)


@pytest.fixture
def spectrum_xml_text() -> str:
    """Load the sanitized Cambium spectrum XML fixture."""
    return FIXTURE_PATH.read_text(encoding="utf-8")


@pytest.fixture
def spectrum_namespaced_xml_text() -> str:
    """Load the namespaced Cambium spectrum XML fixture (issue #78).

    Mirrors ``spectrum_xml_text`` but with
    ``xmlns="http://www.cambiumnetworks.com/spectrum"``. Real Cambium PMP 450i
    radios emit the namespace declaration; the parser must accept both forms.
    """
    return NAMESPACED_FIXTURE_PATH.read_text(encoding="utf-8")


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
        parse_spectrum_xml('<Spectrum_Analyzer><Freq f="3500.0 V" max="-68" /></Spectrum_Analyzer>')


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
# parse_spectrum_xml — Cambium default-namespace tolerance (issue #78)
# ---------------------------------------------------------------------------


def test_parse_spectrum_xml_namespaced_root_returns_32_bins(
    spectrum_namespaced_xml_text: str,
) -> None:
    """Cambium default-namespace XML parses to the same 32 bins as the bare-root fixture.

    Operator-observed shape (PMP 450i firmware 25.1): the root carries
    ``xmlns="http://www.cambiumnetworks.com/spectrum"``. ElementTree renders
    namespaced tags in Clark notation (``{ns}Tag``); the parser must strip
    the namespace prefix before validating the root and before matching
    ``<Freq>`` children.
    """
    bins = parse_spectrum_xml(spectrum_namespaced_xml_text)
    assert len(bins) == 32
    frequencies = sorted({b.frequency_mhz for b in bins})
    assert frequencies == [3500.0, 3540.0, 3550.0, 3560.0, 3600.0, 3620.0, 3650.0, 3700.0]
    # Sanity: all 32 are SpectrumBin instances.
    assert all(isinstance(b, SpectrumBin) for b in bins)


def test_parse_spectrum_xml_namespaced_mixed_polarizations(
    spectrum_namespaced_xml_text: str,
) -> None:
    """The namespaced fixture has 16 V bins and 16 H bins (one of each per frequency row)."""
    bins = parse_spectrum_xml(spectrum_namespaced_xml_text)
    v_count = sum(1 for b in bins if b.polarization == "V")
    h_count = sum(1 for b in bins if b.polarization == "H")
    assert v_count == 16
    assert h_count == 16


def test_parse_spectrum_xml_namespaced_picks_up_avg_max_values(
    spectrum_namespaced_xml_text: str,
) -> None:
    """Attribute parsing still works through the namespace.

    First row of bins matches the bare fixture (3500.0 V/H rows).
    """
    bins = parse_spectrum_xml(spectrum_namespaced_xml_text)
    # First four bins in the fixture are the 3500.0 V/H rows. The parser must
    # yield the same SpectrumBin records whether the root is bare or namespaced.
    assert bins[0] == SpectrumBin(frequency_mhz=3500.0, polarization="V", avg_dbm=-68, max_dbm=-68)
    assert bins[1] == SpectrumBin(frequency_mhz=3500.0, polarization="H", avg_dbm=-65, max_dbm=-64)
    assert bins[2] == SpectrumBin(frequency_mhz=3500.0, polarization="V", avg_dbm=-82, max_dbm=-76)
    assert bins[3] == SpectrumBin(frequency_mhz=3500.0, polarization="H", avg_dbm=-84, max_dbm=-82)


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
# band_range filter — issue #81 (3 GHz CBRS / lightly-licensed band-limit fix).
#
# Cambium PMP 450i 3 GHz hardware (reference C030045A002A) reports
# bins up to ~4200 MHz with an artificial -99 dBm floor above
# 3900 MHz. ``rank_clean_frequencies`` MUST drop those bins BEFORE
# ranking when the ranker is told the radio is a 3 GHz unit, or
# the operator's "cleanest" recommendation gets contaminated by
# out-of-band candidates the radio cannot lock onto.
# ---------------------------------------------------------------------------


def test_rank_clean_frequencies_band_range_filters_out_of_band_bins() -> None:
    """Out-of-band bins are dropped BEFORE ranking when ``band_range`` is supplied.

    Pre-fix bug: the ranker would have recommended the 4050 MHz bin
    as the "cleanest" (avg_dbm=-99), contaminating the operator's
    downstream migration decision. With the filter, only the
    in-band bins (3550, 3600, 3650) participate in the ranking.
    """
    bins = [
        # In-band (CBRS 3500: 3300-3900 MHz)
        SpectrumBin(frequency_mhz=3550.0, polarization="V", avg_dbm=-72, max_dbm=-71),
        SpectrumBin(frequency_mhz=3600.0, polarization="V", avg_dbm=-65, max_dbm=-64),
        SpectrumBin(frequency_mhz=3650.0, polarization="V", avg_dbm=-80, max_dbm=-79),
        # Out-of-band (above 3900 MHz — artificial -99 dBm floor on 3 GHz radio)
        SpectrumBin(frequency_mhz=3950.0, polarization="V", avg_dbm=-99, max_dbm=-99),
        SpectrumBin(frequency_mhz=4050.0, polarization="V", avg_dbm=-99, max_dbm=-99),
        SpectrumBin(frequency_mhz=4150.0, polarization="V", avg_dbm=-99, max_dbm=-99),
    ]
    result = rank_clean_frequencies(bins, top_n=10, band_range=(3300.0, 3900.0))
    # Cleanest first: 3650 (-80) > 3550 (-72) > 3600 (-65)
    assert result == [3650.0, 3550.0, 3600.0]


def test_rank_clean_frequencies_band_range_none_preserves_v1_behaviour() -> None:
    """``band_range=None`` ranks EVERY bin — v1 behaviour preserved.

    Without the filter, the 4050 MHz bin (artificial -99 dBm
    floor on 3 GHz hardware) would score highest. This test pins
    that legacy behaviour so the ranker does not silently change
    the algorithm for callers who do not opt into filtering.
    """
    bins = [
        SpectrumBin(frequency_mhz=3600.0, polarization="V", avg_dbm=-65, max_dbm=-64),
        SpectrumBin(frequency_mhz=4050.0, polarization="V", avg_dbm=-99, max_dbm=-99),
    ]
    assert rank_clean_frequencies(bins, top_n=10) == [4050.0, 3600.0]


def test_rank_clean_frequencies_band_range_all_out_of_band_returns_empty() -> None:
    """Every bin outside the range → empty list (not a crash).

    Guards against a regression where the filter could produce a
    silent empty list when called with a band that does not match
    any bin in the sweep result. The operator should see an
    explicit empty list, not a crash.
    """
    bins = [
        SpectrumBin(frequency_mhz=4050.0, polarization="V", avg_dbm=-99, max_dbm=-99),
        SpectrumBin(frequency_mhz=4150.0, polarization="V", avg_dbm=-99, max_dbm=-99),
    ]
    assert rank_clean_frequencies(bins, top_n=10, band_range=(3300.0, 3900.0)) == []


def test_rank_clean_frequencies_band_range_3ghz_with_realistic_fixture() -> None:
    """Realistic 3 GHz radio sweep: bins 3300-4200 MHz, -99 floor above 3900.

    Mimics the live behaviour observed on C030045A002A hardware
    (issue #81 background). Pre-fix this fixture would have
    recommended 4150.0 (-99 dBm floor) as the "cleanest" candidate.
    Post-fix, the filter restricts ranking to in-band 3500-3900
    frequencies and the operator sees real channel candidates.
    """
    # Build bins: every 10 MHz from 3300 to 4200, V polarization
    # In-band (3500 band): real measurements between -60 and -90 dBm
    # Out-of-band: artificial -99 dBm floor
    bins: list[SpectrumBin] = []
    in_band_floor = {
        3550.0: -70,
        3600.0: -65,
        3650.0: -75,
        3700.0: -68,
        3750.0: -85,
        3800.0: -78,
        3850.0: -82,
    }
    # Sub-CBRS guard bands get a high-noise (-55 dBm) floor so the
    # ranker does not accidentally promote them above real channel
    # candidates. They are inside ``band_range`` but noisy.
    for f in [3300.0, 3350.0, 3400.0, 3450.0, 3500.0] + list(in_band_floor.keys()) + [3900.0]:
        avg = in_band_floor.get(f, -55)
        bins.append(
            SpectrumBin(frequency_mhz=f, polarization="V", avg_dbm=avg, max_dbm=avg + 1)
        )
    for f in [3950.0, 4000.0, 4050.0, 4100.0, 4150.0, 4200.0]:
        bins.append(SpectrumBin(frequency_mhz=f, polarization="V", avg_dbm=-99, max_dbm=-99))

    result = rank_clean_frequencies(bins, top_n=5, band_range=(3300.0, 3900.0))
    # Sorted ascending by (avg_dbm, freq):
    #   (-85, 3750), (-82, 3850), (-78, 3800), (-75, 3650), (-70, 3550)
    # Top 5: 3750, 3850, 3800, 3650, 3550.
    assert result == [3750.0, 3850.0, 3800.0, 3650.0, 3550.0]
    # And NO out-of-band candidate appears:
    assert all(3300.0 <= f <= 3900.0 for f in result)


def test_rank_clean_frequencies_band_range_inclusive_boundaries() -> None:
    """``band_range`` boundaries are inclusive (matches ``_band_for_frequency``).

    A frequency exactly on the lower or upper boundary of the
    supplied range MUST be kept. Mismatches here would silently
    drop the lowest or highest in-band frequency, biasing the
    ranker.
    """
    bins = [
        SpectrumBin(frequency_mhz=3300.0, polarization="V", avg_dbm=-70, max_dbm=-69),
        SpectrumBin(frequency_mhz=3299.0, polarization="V", avg_dbm=-99, max_dbm=-99),
        SpectrumBin(frequency_mhz=3900.0, polarization="V", avg_dbm=-70, max_dbm=-69),
        SpectrumBin(frequency_mhz=3901.0, polarization="V", avg_dbm=-99, max_dbm=-99),
    ]
    result = rank_clean_frequencies(bins, top_n=10, band_range=(3300.0, 3900.0))
    assert result == [3300.0, 3900.0]


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


# ---------------------------------------------------------------------------
# _read_band_range_after_sweep — issue #81 unit tests.
#
# These tests target the helper that translates the radio's
# ``radioFrequencyBand`` OID enum value into a ``(low_mhz, high_mhz)``
# range for the spectrum ranker. The helper is module-private
# (``_``-prefixed) but reachable by direct import so we can pin
# every failure path with a deterministic test.
# ---------------------------------------------------------------------------


class _FakeSnmpClient:
    """Minimal stand-in for the writable SNMP client used in ``fetch_spectrum``.

    Only ``get_oid`` matters for ``_read_band_range_after_sweep``. The
    ``raise_on`` parameter lets each test pin a specific failure mode
    without coupling to any real Cambium client machinery.
    """

    def __init__(self, return_value: object = 1, raise_on: Exception | None = None) -> None:
        self._return_value = return_value
        self._raise_on = raise_on
        self.calls: list[str] = []

    def get_oid(self, oid: str) -> object:
        self.calls.append(oid)
        if self._raise_on is not None:
            raise self._raise_on
        return self._return_value

    def close(self) -> None:
        return None


def _catalog_with(band_oid: str | None) -> OidCatalog:
    """Build a minimal ``OidCatalog`` carrying only the OIDs the helper looks up."""
    oids: dict[str, str] = {
        "spectrumScanDuration": "1.3.6.1.4.1.161.19.3.1.13.1.0",
        "spectrumScanAction": "1.3.6.1.4.1.161.19.3.1.13.2.0",
    }
    if band_oid is not None:
        oids["radioFrequencyBand"] = band_oid
    return OidCatalog(
        version=1,
        vendor="cambium",
        model="pmp450i",
        firmware="25.0.1",
        oids=oids,
        hmac_sha256="",
    )


def test_read_band_range_after_sweep_happy_path_3ghz() -> None:
    """Catalog v2 with ``radioFrequencyBand`` → OID returns ``band3500`` enum value ``1``.

    Maps to ``(3300.0, 3900.0)`` so the spectrum ranker drops
    out-of-band bins above 3900 MHz.
    """
    client = _FakeSnmpClient(return_value=1)
    catalog = _catalog_with("1.3.6.1.4.1.161.19.3.3.16.1.1.2")
    result = _read_band_range_after_sweep(client=client, catalog=catalog)
    assert result == (3300.0, 3900.0)
    assert client.calls == ["1.3.6.1.4.1.161.19.3.3.16.1.1.2"]


def test_read_band_range_after_sweep_happy_path_5ghz() -> None:
    """Catalog v2 on 5 GHz hardware → OID returns ``band5700`` enum value ``6``.

    Maps to ``(5725.0, 5875.0)``. Proves the helper preserves v1
    behaviour on the dominant fleet.
    """
    client = _FakeSnmpClient(return_value=6)
    catalog = _catalog_with("1.3.6.1.4.1.161.19.3.3.16.1.1.2")
    result = _read_band_range_after_sweep(client=client, catalog=catalog)
    assert result == (5725.0, 5875.0)


def test_read_band_range_after_sweep_band5800_folds_into_band5700() -> None:
    """Cambium enum value ``7`` (band5800) folds into ``band5700`` (5725-5875 overlap).

    Documents the WHISP-BOX-MIBV2-MIB alias: ``band5800`` and
    ``band5700`` share the 5725-5875 range, so the helper must NOT
    return two different ranges.
    """
    client = _FakeSnmpClient(return_value=7)
    catalog = _catalog_with("1.3.6.1.4.1.161.19.3.3.16.1.1.2")
    assert _read_band_range_after_sweep(client=client, catalog=catalog) == (5725.0, 5875.0)


def test_read_band_range_after_sweep_string_coerced_enum() -> None:
    """Some agents coerce ``INTEGER`` to a ``str`` on the wire; the helper MUST coerce back.

    Cambium firmware returns ``INTEGER`` per WHISP-BOX-MIBV2-MIB but
    intermediate SNMP libraries sometimes serialise it as a string.
    The helper accepts both via ``int | str`` typing.
    """
    client = _FakeSnmpClient(return_value="2")  # band4900
    catalog = _catalog_with("1.3.6.1.4.1.161.19.3.3.16.1.1.2")
    assert _read_band_range_after_sweep(client=client, catalog=catalog) == (4900.0, 5000.0)


def test_read_band_range_after_sweep_catalog_missing_oid_returns_none() -> None:
    """Legacy catalog without ``radioFrequencyBand`` OID → ``None`` (no filter).

    The helper MUST NOT crash; it MUST return ``None`` and let
    ``fetch_spectrum`` fall back to the v1 behaviour of trusting
    the spectrum analyser data as ground truth.
    """
    client = _FakeSnmpClient(return_value=1)  # would map to band3500 if reached
    catalog = _catalog_with(band_oid=None)
    assert _read_band_range_after_sweep(client=client, catalog=catalog) is None
    assert client.calls == [], (
        "Helper MUST short-circuit before the GET when the catalog lacks "
        "the OID. The catalog-missing path is informational-only; reading "
        "a non-existent OID would waste a wire frame and risk a timeout."
    )


def test_read_band_range_after_sweep_unrecognised_enum_returns_none() -> None:
    """``unknown`` sentinel (``0``) or future-firmware value → ``None`` (no filter).

    Proves the helper does NOT silently default to a band when the
    integer is not in the recognised Cambium enum table.
    """
    for unknown_value in (0, 99, -1):
        client = _FakeSnmpClient(return_value=unknown_value)
        catalog = _catalog_with("1.3.6.1.4.1.161.19.3.3.16.1.1.2")
        assert (
            _read_band_range_after_sweep(client=client, catalog=catalog) is None
        ), f"unrecognised enum value {unknown_value!r} MUST return None"


def test_read_band_range_after_sweep_get_failure_returns_none() -> None:
    """GET on ``radioFrequencyBand`` raises → ``None`` (no filter) without crashing.

    Failure policy: ANY exception from the GET is defensive — the
    sweep is already COMPLETED, so the helper falls back to no
    filter rather than crashing the post-sweep ladder.
    """
    client = _FakeSnmpClient(
        return_value=1,
        raise_on=TimeoutError("agent unreachable during band GET"),
    )
    catalog = _catalog_with("1.3.6.1.4.1.161.19.3.3.16.1.1.2")
    assert _read_band_range_after_sweep(client=client, catalog=catalog) is None
    assert client.calls == ["1.3.6.1.4.1.161.19.3.3.16.1.1.2"]


def test_read_band_range_after_sweep_uncoercible_string_returns_none() -> None:
    """A GET result that cannot be coerced to ``int`` → ``None`` (no filter).

    Defensive guard against future Cambium firmware that might return
    a non-numeric token for the OID (e.g. ``"unknown"``).
    """
    client = _FakeSnmpClient(return_value="not-a-number")
    catalog = _catalog_with("1.3.6.1.4.1.161.19.3.3.16.1.1.2")
    assert _read_band_range_after_sweep(client=client, catalog=catalog) is None


def test_noise_floor_per_channel_is_unfiltered_by_design() -> None:
    """``noise_floor_per_channel`` returns ALL bins, INCLUDING out-of-band ones.

    Issue #81 design choice (pinned here to prevent future drift):
    the band_range filter is applied ONLY by ``rank_clean_frequencies``
    (which the operator uses to pick a candidate). The noise floor
    map is a reference of every measurement the radio reported; it
    intentionally includes out-of-band readings so the operator can
    see WHY a candidate was excluded (e.g. the -99 dBm floor above
    3900 MHz on 3 GHz hardware). Applying the filter to BOTH would
    hide the diagnostic signal.

    If a future contributor wants to apply the filter to the noise
    floor dict too, they MUST update this test first.
    """
    bins = [
        # In-band (3500 band: 3300-3900)
        SpectrumBin(frequency_mhz=3600.0, polarization="V", avg_dbm=-65, max_dbm=-64),
        # Out-of-band (>3900 — 3 GHz artificial floor)
        SpectrumBin(frequency_mhz=4050.0, polarization="V", avg_dbm=-99, max_dbm=-99),
    ]
    noise = noise_floor_per_channel(bins)
    # BOTH frequencies appear — no filtering here:
    assert set(noise.keys()) == {"3600.0", "4050.0"}
    assert noise["4050.0"] == -99
    # And rank_clean_frequencies DOES filter (separate test above):
    ranked = rank_clean_frequencies(bins, top_n=10, band_range=(3300.0, 3900.0))
    assert ranked == [3600.0]


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
    "test_parse_spectrum_xml_namespaced_root_returns_32_bins",
    "test_parse_spectrum_xml_namespaced_mixed_polarizations",
    "test_parse_spectrum_xml_namespaced_picks_up_avg_max_values",
    "test_rank_clean_frequencies_empty_returns_empty",
    "test_rank_clean_frequencies_orders_ascending_by_worst_leg",
    "test_rank_clean_frequencies_top_n_cap",
    "test_rank_clean_frequencies_ties_broken_by_frequency_ascending",
    "test_noise_floor_per_channel_empty_returns_empty",
    "test_noise_floor_per_channel_worst_leg_per_frequency",
    "test_spectrum_http_errors_inherit_driver_error",
    "test_read_band_range_after_sweep_happy_path_3ghz",
    "test_read_band_range_after_sweep_happy_path_5ghz",
    "test_read_band_range_after_sweep_band5800_folds_into_band5700",
    "test_read_band_range_after_sweep_string_coerced_enum",
    "test_read_band_range_after_sweep_catalog_missing_oid_returns_none",
    "test_read_band_range_after_sweep_unrecognised_enum_returns_none",
    "test_read_band_range_after_sweep_get_failure_returns_none",
    "test_read_band_range_after_sweep_uncoercible_string_returns_none",
    "test_noise_floor_per_channel_is_unfiltered_by_design",
]
