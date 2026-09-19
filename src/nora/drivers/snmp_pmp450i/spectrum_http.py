"""Post-sweep `SpectrumAnalysis.xml` HTTP fetch + RF bin decoding — issue #70.

After the real Cambium WHISP-BOX-MIBV2-MIB sweep protocol completes
(``.221.0 == 4`` — ``idleCompleteSpectrumAnalysis``), the radio
publishes a per-device XML file at ``http://{ip}/SpectrumAnalysis.xml``
on its web root. The file carries one ``<Freq f="..." avg="..." max="..." />``
element per (frequency, polarization) sample: 722 elements per radio on
the operator's production hardware (361 frequencies × 2 polarizations
V and H).

Issue #62 / WU-3 left ``ranked_clean_frequencies`` and
``noise_floor_dbm`` as empty placeholders on ``SpectrumSweepResult``.
This module fills them in:

1. :func:`fetch_spectrum_xml` — HTTP GET against the radio's web root
   with bounded retry / timeout. Defends against the radio's web
   server being slow to start after a sweep (operator observation:
   ~5-15s of HTTP silence while the radio reloads).
2. :func:`parse_spectrum_xml` — stdlib ``ElementTree`` parse of the
   ``<Spectrum_Analyzer>`` root + ``<Freq>`` children.
3. :class:`SpectrumBin` — typed (frequency, polarization, avg, max)
   Pydantic model. Frozen.
4. :func:`rank_clean_frequencies` — "worst-case min first" ranking:
   per frequency the worst-leg ``avg_dbm`` is the channel's noise
   proxy; sort ascending (most negative = cleanest first); return top
   ``n`` frequencies in MHz.
5. :func:`noise_floor_per_channel` — dict keyed by ``"{freq_mhz:.1f}"``
   carrying the worst-leg ``avg_dbm`` per channel.

Tier-1 ``operator_confirmed`` gate is enforced at the call site (in
``spectrum.fetch_spectrum``), not here — the HTTP fetch is a
post-sweep consumer of the sweep result, not a stand-alone MCP tool.

Zero-Leakage: the ``host`` string is the inventory device's IPv4
literal (already TEST-NET-1 sanitised at the inventory layer); the
URL builder does not embed any other identifier.
"""

from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from typing import Final, Literal

import httpx
from pydantic import BaseModel, ConfigDict

from nora.drivers.exceptions import SpectrumHttpFetchError, SpectrumXmlParseError

logger = logging.getLogger("nora.drivers.snmp_pmp450i.spectrum_http")


# ---------------------------------------------------------------------------
# Typed model — issue #70 WU-1
# ---------------------------------------------------------------------------


Polarization = Literal["V", "H"]


class SpectrumBin(BaseModel):
    """One frequency-polarization sample from ``SpectrumAnalysis.xml``.

    Mirrors the operator-observed ``<Freq f="{freq_mhz} {pol}" avg="{avg_dbm}" max="{max_dbm}" />``
    shape (one decimal precision on frequency, integer dBm on
    avg / max, single-letter polarization V or H).

    Frozen — bins are immutable observations; aggregation is computed
    at fold time by :func:`noise_floor_per_channel` and
    :func:`rank_clean_frequencies`.
    """

    model_config = ConfigDict(frozen=True)

    frequency_mhz: float
    polarization: Polarization
    avg_dbm: int
    max_dbm: int


# ---------------------------------------------------------------------------
# URL builder — issue #70 WU-1
# ---------------------------------------------------------------------------


# Defaults match the operator's physical-hardware observation on Cambium
# PMP 450i firmware 25.0.1: plain HTTP on port 80, path
# ``/SpectrumAnalysis.xml``.
DEFAULT_SPECTRUM_SCHEME: Final[str] = "http"
DEFAULT_SPECTRUM_PORT: Final[int] = 80
DEFAULT_SPECTRUM_PATH: Final[str] = "/SpectrumAnalysis.xml"


def build_spectrum_url(
    host: str,
    *,
    scheme: str = DEFAULT_SPECTRUM_SCHEME,
    port: int = DEFAULT_SPECTRUM_PORT,
    path: str = DEFAULT_SPECTRUM_PATH,
) -> str:
    """Build the spectrum-analysis XML URL for one radio.

    The Cambium radio exposes the XML on its plain-HTTP web root
    (no TLS). Tests override ``scheme`` / ``port`` / ``path`` to
    exercise unusual deployments (HTTPS front-end, reverse proxy on
    a non-standard port, etc.).
    """
    if not host:
        raise ValueError("host must be a non-empty string")
    return f"{scheme}://{host}:{port}{path}"


# ---------------------------------------------------------------------------
# HTTP fetch with bounded retry — issue #70 WU-1
# ---------------------------------------------------------------------------


# Retryable status codes for the spectrum XML endpoint. 4xx is treated
# as fatal (operator misconfiguration: wrong host / wrong path / wrong
# firmware); 5xx and 408 / 429 are retried because the radio's web
# server is slow to start after a sweep.
_RETRYABLE_STATUS_CODES: Final[frozenset[int]] = frozenset({408, 429, 500, 502, 503, 504})


def fetch_spectrum_xml(
    host: str,
    *,
    http_client: httpx.Client | None = None,
    timeout_seconds: float = 10.0,
    max_retries: int = 5,
    retry_delay_seconds: float = 3.0,
    scheme: str = DEFAULT_SPECTRUM_SCHEME,
    port: int = DEFAULT_SPECTRUM_PORT,
    path: str = DEFAULT_SPECTRUM_PATH,
) -> str:
    """GET the spectrum-analysis XML for one radio, with bounded retry.

    ``max_retries=5`` × ``retry_delay_seconds=3.0`` = 15s of patience
    before failing — covers the operator-observed ~5-15s of HTTP
    silence while the radio reloads its web server post-sweep.
    ``max_retries=0`` makes exactly one attempt (no retry).

    On retry exhaustion, or on a non-retryable HTTP status (4xx other
    than 408/429), raises :class:`SpectrumHttpFetchError`. The
    exception carries the offending ``host`` so the MCP tool boundary
    can produce a typed diagnostic without leaking the URL (which
    duplicates the host verbatim).

    Pass ``http_client`` to inject a pre-built ``httpx.Client`` (used
    by tests via ``httpx.MockTransport``). When omitted, a one-shot
    client is created and closed inside this call.
    """
    url = build_spectrum_url(host, scheme=scheme, port=port, path=path)
    last_error: Exception | None = None
    last_status: int | None = None

    # Total attempts = 1 initial + max_retries. With max_retries=0 we
    # still get one attempt; the loop below iterates ``max_retries + 1`` times.
    for attempt in range(max_retries + 1):
        owns_client = http_client is None
        client = http_client if http_client is not None else httpx.Client(timeout=timeout_seconds)
        try:
            try:
                response = client.get(url)
            except httpx.TimeoutException as exc:
                last_error = exc
                logger.debug(
                    "spectrum HTTP fetch timeout on attempt %d/%d (host=%s)",
                    attempt + 1,
                    max_retries + 1,
                    host,
                )
            except httpx.ConnectError as exc:
                last_error = exc
                logger.debug(
                    "spectrum HTTP fetch connect error on attempt %d/%d (host=%s): %s",
                    attempt + 1,
                    max_retries + 1,
                    host,
                    exc,
                )
            except httpx.HTTPError as exc:
                # Catch-all for other httpx errors (ReadError, RemoteProtocolError, ...).
                last_error = exc
                logger.debug(
                    "spectrum HTTP fetch error on attempt %d/%d (host=%s): %s",
                    attempt + 1,
                    max_retries + 1,
                    host,
                    exc,
                )
            else:
                if response.status_code == 200:
                    return response.text
                last_status = response.status_code
                if response.status_code not in _RETRYABLE_STATUS_CODES:
                    # Fatal — do not retry.
                    raise SpectrumHttpFetchError(
                        host=host,
                        status_code=response.status_code,
                        attempts=attempt + 1,
                        message=(
                            f"spectrum HTTP fetch failed on host={host!r} "
                            f"with status={response.status_code} "
                            f"(non-retryable)"
                        ),
                    )
                last_error = SpectrumHttpFetchError(
                    host=host,
                    status_code=response.status_code,
                    attempts=attempt + 1,
                    message=(
                        f"spectrum HTTP fetch failed on host={host!r} "
                        f"with status={response.status_code} "
                        f"(retryable, will retry)"
                    ),
                )
                logger.debug(
                    "spectrum HTTP fetch status=%d on attempt %d/%d (host=%s)",
                    response.status_code,
                    attempt + 1,
                    max_retries + 1,
                    host,
                )
        finally:
            if owns_client:
                try:
                    client.close()
                except Exception:  # pragma: no cover — close is best-effort
                    pass

        # Sleep between attempts (NOT after the final one). Defensive
        # against negative or zero delays — sleep(0) is a yield, no harm.
        if attempt < max_retries:
            time.sleep(max(0.0, float(retry_delay_seconds)))

    # Retry exhausted.
    if last_status is not None:
        raise SpectrumHttpFetchError(
            host=host,
            status_code=last_status,
            attempts=max_retries + 1,
            message=(
                f"spectrum HTTP fetch exhausted {max_retries + 1} attempts "
                f"on host={host!r}; last status={last_status}"
            ),
        )
    raise SpectrumHttpFetchError(
        host=host,
        status_code=0,
        attempts=max_retries + 1,
        message=(
            f"spectrum HTTP fetch exhausted {max_retries + 1} attempts "
            f"on host={host!r}; last error={last_error!r}"
        ),
    )


# ---------------------------------------------------------------------------
# XML parser — issue #70 WU-1
# ---------------------------------------------------------------------------


_SPECTRUM_ANALYZER_ROOT: Final[str] = "Spectrum_Analyzer"
_SPECTRUM_FREQ_ELEMENT: Final[str] = "Freq"
_SPECTRUM_FREQ_ATTR_F: Final[str] = "f"
_SPECTRUM_FREQ_ATTR_AVG: Final[str] = "avg"
_SPECTRUM_FREQ_ATTR_MAX: Final[str] = "max"


def parse_spectrum_xml(xml_text: str) -> list[SpectrumBin]:
    """Parse a ``SpectrumAnalysis.xml`` payload into ``SpectrumBin`` records.

    The expected schema (operator-verified on Cambium PMP 450i firmware
    25.0.1 / 25.1):

    .. code-block:: xml

        <Spectrum_Analyzer>
          <Freq f="3500.0 V" avg="-68" max="-68" />
          <Freq f="3500.0 H" avg="-65" max="-64" />
          ...
        </Spectrum_Analyzer>

    Empty ``<Spectrum_Analyzer></Spectrum_Analyzer>`` is valid and
    returns ``[]`` (the sweep ran but no bins were captured — operator
    hardware verification: sentinel 3 ``idleNoSpectrumAnalysis`` on the
    AP after a sweep with unusable results).

    Malformed XML (no root, missing required attributes, unknown
    polarization, non-numeric avg/max, non-positive frequency) raises
    :class:`SpectrumXmlParseError`.
    """
    if not xml_text or not xml_text.strip():
        raise SpectrumXmlParseError(
            message="empty spectrum XML payload",
            line=0,
        )
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise SpectrumXmlParseError(
            message=f"malformed XML: {exc}",
            line=int(getattr(exc, "position", (0, 0))[0]) if getattr(exc, "position", None) else 0,
        ) from exc

    if root.tag != _SPECTRUM_ANALYZER_ROOT:
        raise SpectrumXmlParseError(
            message=(
                f"unexpected root element <{root.tag}>; "
                f"expected <{_SPECTRUM_ANALYZER_ROOT}>"
            ),
            line=0,
        )

    bins: list[SpectrumBin] = []
    for freq_el in root.findall(_SPECTRUM_FREQ_ELEMENT):
        try:
            f_attr = freq_el.get(_SPECTRUM_FREQ_ATTR_F, "")
            avg_attr = freq_el.get(_SPECTRUM_FREQ_ATTR_AVG, "")
            max_attr = freq_el.get(_SPECTRUM_FREQ_ATTR_MAX, "")
            if not f_attr or not avg_attr or not max_attr:
                raise SpectrumXmlParseError(
                    message=(
                        f"<Freq> missing required attribute; "
                        f"f={f_attr!r} avg={avg_attr!r} max={max_attr!r}"
                    ),
                    line=0,
                )
            # Format: "{freq_mhz} {pol}" — split on the LAST whitespace
            # so frequencies with internal whitespace (defensive) still
            # parse. Operator's firmware emits one decimal + single space.
            f_attr_stripped = f_attr.strip()
            parts = f_attr_stripped.rsplit(None, 1)
            if len(parts) != 2:
                raise SpectrumXmlParseError(
                    message=(
                        f"<Freq f={f_attr_stripped!r}> must be "
                        f"'<freq_mhz> <polarization>' (V or H)"
                    ),
                    line=0,
                )
            freq_str, pol_str = parts[0].strip(), parts[1].strip()
            freq_mhz = float(freq_str)
            pol = pol_str.upper()
            if pol not in ("V", "H"):
                raise SpectrumXmlParseError(
                    message=(
                        f"<Freq f={f_attr_stripped!r}> polarization must be V or H; got {pol!r}"
                    ),
                    line=0,
                )
            avg_dbm = int(avg_attr)
            max_dbm = int(max_attr)
            if freq_mhz <= 0:
                raise SpectrumXmlParseError(
                    message=(
                        f"<Freq f={f_attr_stripped!r}> frequency must be positive; got {freq_mhz}"
                    ),
                    line=0,
                )
        except SpectrumXmlParseError:
            raise
        except (ValueError, TypeError) as exc:
            raise SpectrumXmlParseError(
                message=(
                    f"<Freq> attribute parse failure: "
                    f"f={freq_el.get('f', '')!r} avg={freq_el.get('avg', '')!r} "
                    f"max={freq_el.get('max', '')!r} ({exc})"
                ),
                line=0,
            ) from exc

        bins.append(
            SpectrumBin(
                frequency_mhz=freq_mhz,
                polarization=pol,  # type: ignore[arg-type]  # narrowed to Literal["V", "H"]
                avg_dbm=avg_dbm,
                max_dbm=max_dbm,
            )
        )

    return bins


# ---------------------------------------------------------------------------
# Ranking + noise floor — issue #70 WU-2 (helpers; called by spectrum.py)
# ---------------------------------------------------------------------------


def noise_floor_per_channel(bins: list[SpectrumBin]) -> dict[str, float]:
    """Compute the per-channel worst-leg noise floor.

    For each unique frequency ``f``, the "worst-leg avg" is the
    maximum ``avg_dbm`` across all bins at that frequency (the leg
    with the highest noise — operator's RF ranking proxy). More
    negative = cleaner.

    Returns a dict keyed by ``"{freq_mhz:.1f}"`` (string, stable for
    JSON serialisation) with value ``float(worst_avg_dbm)``.

    Empty input → empty dict.
    """
    if not bins:
        return {}
    worst_per_freq: dict[float, int] = {}
    for bin_ in bins:
        freq = bin_.frequency_mhz
        if freq not in worst_per_freq or bin_.avg_dbm > worst_per_freq[freq]:
            worst_per_freq[freq] = bin_.avg_dbm
    return {f"{freq:.1f}": float(worst) for freq, worst in sorted(worst_per_freq.items())}


def rank_clean_frequencies(
    bins: list[SpectrumBin],
    *,
    top_n: int = 10,
) -> list[float]:
    """Rank frequencies by worst-leg avg_dbm; return the top ``n`` cleanest.

    Algorithm ("worst-case min first"):

    1. For each unique frequency ``f``, compute
       ``worst_avg = max(avg_dbm across all bins at f)`` — the channel's
       worst-leg noise proxy.
    2. Sort unique frequencies ascending by ``worst_avg`` (most
       negative = cleanest first; ties broken by frequency ascending).
    3. Return the top ``n`` frequency values (MHz) as a flat list.

    ``top_n`` defaults to 10; callers (the spectrum helper) override
    via ``Settings.nora_spectrum_ranking_top_n``.

    Empty input → empty list.
    """
    if not bins or top_n <= 0:
        return []
    worst_per_freq: dict[float, int] = {}
    for bin_ in bins:
        freq = bin_.frequency_mhz
        if freq not in worst_per_freq or bin_.avg_dbm > worst_per_freq[freq]:
            worst_per_freq[freq] = bin_.avg_dbm
    ranked = sorted(worst_per_freq.items(), key=lambda pair: (pair[1], pair[0]))
    return [float(freq) for freq, _ in ranked[:top_n]]


__all__ = [
    "SpectrumBin",
    "Polarization",
    "build_spectrum_url",
    "fetch_spectrum_xml",
    "parse_spectrum_xml",
    "noise_floor_per_channel",
    "rank_clean_frequencies",
    "DEFAULT_SPECTRUM_SCHEME",
    "DEFAULT_SPECTRUM_PORT",
    "DEFAULT_SPECTRUM_PATH",
]
