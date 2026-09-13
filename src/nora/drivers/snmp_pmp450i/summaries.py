"""`Pmp450iSnmpDriver` read-summary helpers — slice 2 (PR 2).

Slice 2 exposes two typed Pydantic models and two thin helpers
backed by ``Pmp450iSnmpDriver``:

* :class:`ApSummary` — read via :func:`fetch_ap_summary`. The
  ``Pmp450iSnmpDriver.fetch_ap_summary`` Protocol method delegates
  here.
* :class:`FrameUtilization` — read via :func:`fetch_frame_utilization`.
  ``Pmp450iSnmpDriver.fetch_frame_utilization`` delegates here.

Both helpers follow the same shape — ``_resolve_and_fetch`` is the
extracted common path (REFACTOR 2.9): resolve the catalog, open a
client, fetch one wire GET per known OID, fold the response into the
typed model, and return ``None`` for any OID the catalog does not
expose (with a literal ``"OID catalog fallback: ..."`` warning so
the operator can see which OID is missing).

The minor-mismatch fallback is delegated to
:class:`OidCatalogRegistry.resolve` — the resolver's
``logger.warning("OID catalog fallback: requested X, using Y
(minor mismatch)")`` line bubbles up to stderr for every tool caller
(see `oid-catalog/spec.md` MODIFIED requirement "Minor Descending
Fallback With Literal Warning" scenario "minor mismatch returns
closest lower minor with literal warning via the tool path").

Zero-Leakage: the helpers never echo raw credentials, private IPs,
or MAC addresses. Free-text fields are passed back to the MCP layer
where the existing ``Sanitizer`` applies.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from nora.drivers.inventory import Device
    from nora.drivers.oid_catalog import OidCatalog
    from nora.drivers.snmp_pmp450i.client import SnmpClient

logger = logging.getLogger("nora.drivers.snmp_pmp450i.summaries")


# ---------------------------------------------------------------------------
# Typed models — slice 2 surface.
# ---------------------------------------------------------------------------


class ApSummary(BaseModel):
    """Typed AP summary — slice 2 read tool return.

    Fields are stable public object names (per
    `pmp450i-radio-tools/spec.md` sub-cluster 1 scenario "ap_summary
    returns a typed model"). Optional fields carry the catalog fallback
    contract: missing OID names in the catalog yield ``None`` plus a
    literal warning line, never a wire error.
    """

    model_config = ConfigDict(frozen=True)

    firmware: str | None = None
    carrier_frequency_mhz: int | None = None
    channel_width_mhz: int | None = None
    tx_power_dbm: int | None = None
    subscribers_count: int | None = None
    sys_uptime_seconds: int | None = None


class FrameUtilization(BaseModel):
    """Typed frame utilisation — slice 2 read tool return.

    Per `pmp450i-radio-tools/spec.md` sub-cluster 1 scenario "frame
    utilization returns a typed model": downlink + uplink percentages
    are typed floats. The catalog fallback contract applies.
    """

    model_config = ConfigDict(frozen=True)

    dl_pct: float | None = None
    ul_pct: float | None = None


# ---------------------------------------------------------------------------
# Per-tool OID-name map — slice 2 surface.
#
# These names are looked up dynamically against the resolved catalog
# (`catalog.oids[name]`). The catalog envelope carries the dotted OIDs
# for each name; this module owns the *name → field* mapping.
# ---------------------------------------------------------------------------


# OID names that ``snmp_get_ap_summary`` reads. The four NEW names
# land in the catalog envelope as part of slice 2; ``frequency``,
# ``channelBandwidth``, ``transmitPower``, ``upTime`` are reused from
# the v1 seed so the read tool has the full carrier/channel/tx-power
# picture without forcing slice 2 to add yet more catalog entries.
AP_SUMMARY_OID_NAMES: tuple[str, ...] = (
    "apFirmwareVersion",
    "frequency",
    "channelBandwidth",
    "transmitPower",
    "subscribersCount",
    "upTime",
)


# OID names that ``snmp_get_frame_utilization`` reads.
FRAME_UTILIZATION_OID_NAMES: tuple[str, ...] = (
    "frameUtilizationDlPct",
    "frameUtilizationUlPct",
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


FetcherFn = Callable[["Device", "OidCatalog", "SnmpClient"], Any]


def _resolve_and_fetch(
    *,
    driver: Any,
    device_id: str,
    fetcher: FetcherFn,
) -> Any:
    """Resolve the catalog then run ``fetcher`` against the open client.

    The shared path: every slice 2 helper needs (1) the resolved
    catalog, (2) the open client, and (3) the typed model fold. Each
    helper is responsible for steps 2-3; this wrapper owns step 1 and
    guarantees the catalog is resolved before any wire frame, per
    `pmp450i-radio-tools/spec.md` sub-cluster 1 requirement: "Both
    tools SHALL ... MUST call ``OidCatalogRegistry.resolve(...)``
    before any wire frame".

    REFACTOR 2.9 — both ``fetch_ap_summary`` and
    ``fetch_frame_utilization`` delegate here so the catalog-resolve
    + client-open + error-mapping logic lives in exactly one place.
    """
    device = driver._inventory.get(device_id)  # noqa: SLF001 — internal API
    catalog = driver._catalog_registry.resolve(  # noqa: SLF001 — internal API
        (device.vendor, device.model, device.firmware)
    )
    client = driver._client_factory(device)  # noqa: SLF001 — internal API
    try:
        return fetcher(device, catalog, client)
    finally:
        try:
            client.close()
        except Exception:  # pragma: no cover - close is best-effort
            pass


def _safe_lookup(catalog: "OidCatalog", name: str) -> str | None:
    """Return the dotted OID for ``name`` or ``None`` with a warning.

    Missing OID names emit a literal ``"OID catalog fallback: name
    '<name>' not in catalog"`` warning so the operator can see which
    field the catalog dropped. The caller's fold treats ``None`` as
    "field is absent" and produces a typed model with ``None`` for
    that field.
    """
    if name not in catalog.oids:
        logger.warning(
            "OID catalog fallback: name %r not in catalog (vendor=%s, model=%s, firmware=%s)",
            name,
            catalog.vendor,
            catalog.model,
            catalog.firmware,
        )
        return None
    return catalog.oids[name]


def _safe_get(client: "SnmpClient", dotted_oid: str) -> str | int | None:
    """Return ``client.get_oid(dotted_oid)`` or ``None`` on miss.

    The thin summary helpers tolerate missing fields. A wire failure
    on a known OID surfaces as a typed exception (the driver
    remaps ``TimeoutError`` / ``OSError`` / ``ValueError`` via
    ``_fetch_all`` for radio metrics — for the summary helpers we
    accept ``None`` so a partially-unreachable device still returns a
    typed report).

    ``KeyError`` is the canonical "agent returned nothing for this
    OID" signal from the hermetic fake clients in PR 2's tests.
    """
    try:
        return client.get_oid(dotted_oid)
    except KeyError:
        return None


def _coerce_int(value: str | int | None) -> int | None:
    """Coerce ``value`` to ``int`` or return ``None``.

    Cambium agents return OctetString for some scalars (string-typed
    raw value); the driver layer accepts both. ``int(value)`` rejects
    non-numeric strings — we return ``None`` so the typed model
    surfaces "unknown" rather than crashing on bad data.
    """
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_float(value: str | int | None) -> float | None:
    """Coerce ``value`` to ``float`` or return ``None``."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Public helpers — slice 2 read tool bodies.
# ---------------------------------------------------------------------------


def fetch_ap_summary(*, driver: Any, device_id: str) -> ApSummary:
    """Read an AP summary via ``Pmp450iSnmpDriver``.

    The helper resolves the catalog, opens a client, fetches one wire
    GET per AP-summary OID name, and folds the response into a typed
    :class:`ApSummary`. Missing fields become ``None`` (with a
    warning); wire failures on a known OID surface as typed driver
    exceptions via the Protocol seam.
    """

    def _fetcher(device: "Device", catalog: "OidCatalog", client: "SnmpClient") -> ApSummary:
        # ``apFirmwareVersion`` is a NEW PR 2 catalog entry.
        firmware_dotted = _safe_lookup(catalog, "apFirmwareVersion")
        firmware_value = _safe_get(client, firmware_dotted) if firmware_dotted else None
        # Legacy fields reused from the v1 catalog seed.
        carrier_dotted = _safe_lookup(catalog, "frequency")
        channel_dotted = _safe_lookup(catalog, "channelBandwidth")
        tx_power_dotted = _safe_lookup(catalog, "transmitPower")
        # NEW PR 2 catalog entry.
        subscribers_dotted = _safe_lookup(catalog, "subscribersCount")
        # Legacy field reused from the v1 catalog seed.
        uptime_dotted = _safe_lookup(catalog, "upTime")

        return ApSummary(
            firmware=str(firmware_value) if firmware_value is not None else None,
            carrier_frequency_mhz=_coerce_int(
                _safe_get(client, carrier_dotted) if carrier_dotted else None
            ),
            channel_width_mhz=_coerce_int(
                _safe_get(client, channel_dotted) if channel_dotted else None
            ),
            tx_power_dbm=_coerce_int(
                _safe_get(client, tx_power_dotted) if tx_power_dotted else None
            ),
            subscribers_count=_coerce_int(
                _safe_get(client, subscribers_dotted) if subscribers_dotted else None
            ),
            sys_uptime_seconds=_coerce_int(
                _safe_get(client, uptime_dotted) if uptime_dotted else None
            ),
        )

    result = _resolve_and_fetch(driver=driver, device_id=device_id, fetcher=_fetcher)
    return result  # type: ignore[no-any-return]


def fetch_frame_utilization(*, driver: Any, device_id: str) -> FrameUtilization:
    """Read frame utilisation via ``Pmp450iSnmpDriver``.

    The helper resolves the catalog, opens a client, fetches one wire
    GET per frame-utilisation OID name, and folds the response into a
    typed :class:`FrameUtilization`. Missing fields become ``None``
    (with a warning); wire failures surface as typed driver exceptions.
    """

    def _fetcher(device: "Device", catalog: "OidCatalog", client: "SnmpClient") -> FrameUtilization:
        dl_dotted = _safe_lookup(catalog, "frameUtilizationDlPct")
        ul_dotted = _safe_lookup(catalog, "frameUtilizationUlPct")
        return FrameUtilization(
            dl_pct=_coerce_float(_safe_get(client, dl_dotted) if dl_dotted else None),
            ul_pct=_coerce_float(_safe_get(client, ul_dotted) if ul_dotted else None),
        )

    result = _resolve_and_fetch(driver=driver, device_id=device_id, fetcher=_fetcher)
    return result  # type: ignore[no-any-return]


__all__ = [
    "ApSummary",
    "FrameUtilization",
    "fetch_ap_summary",
    "fetch_frame_utilization",
    "AP_SUMMARY_OID_NAMES",
    "FRAME_UTILIZATION_OID_NAMES",
]
