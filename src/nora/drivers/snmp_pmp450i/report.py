"""`RadioMetricsReport` — typed return for the driver.

The model has NO `dict` or `Any` field; every entry is a typed scalar.
`fold` is the only way to construct one — it takes a `Device`, the
resolved `OidCatalog`, the raw `(oid -> str) value` map from the wire,
and an explicit `fetched_at` timestamp.

The fold logic is a pure function: same inputs always produce the same
`RadioMetricsReport`. `NetworkUnreachableError` is raised when a value
cannot be parsed (e.g. non-numeric on a numeric OID) — that mirrors the
typed-error contract from Driver-R5.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Final

from pydantic import BaseModel, ConfigDict

from nora.drivers.exceptions import NetworkUnreachableError
from nora.drivers.inventory import Device
from nora.drivers.oid_catalog import OidCatalog

# Mapping from OID catalog public name -> ``RadioMetricsReport`` field
# name + the per-field parser that converts the wire value into the
# field's typed scalar. The mapping is one-to-one (each OID name maps to
# exactly one field) and the parser dispatch lives in ``fold``.
#
# Issue #57 (2026-09-19): the legacy radio-metrics subset
# (``radioDownlinkRate`` / ``radioUplinkRate`` /
# ``signalStrengthRx`` / ``ssr`` / ``modulationMode``) was removed from
# this map — every one of those OIDs points at a per-LUID
# ``whispLinkEntry`` tabular column whose ``.0`` instance returns
# ``noSuchName`` on real Cambium PMP 450i hardware. The map now lists
# only sector-level scalars (``whispBox*`` / ``whispApsRFConfigRadio*``
# trees) that return live values against production firmware 25.0.1.
_OID_NAME_TO_FIELD: Final[dict[str, str]] = {
    # ``whispBoxActiveEIRP`` (.306.0) returns ``"44 dBm"``. Parser
    # strips the unit and stores the integer dBm value. Required
    # by the boot-time gate (``REQUIRED_OIDS``) — the only sector
    # scalar every PMP 450i catalog carries.
    "eirp": "eirp_dbm",
    # ``whispBoxActiveTxPowerInHundredthsDbm`` (.233.0) returns
    # ``Integer32`` in hundredths of dBm (e.g. ``2700`` -> ``27.0 dBm``).
    # Optional — only populated when the resolved catalog carries it.
    "activeTxPowerDbh": "active_tx_power_dbm",
    # ``channelBandwidth`` (.83.0) returns ``DisplayString`` like
    # ``"20.0"`` (MHz, fractional). Optional.
    "channelBandwidth": "channel_bandwidth_mhz",
    # ``radioFreqCarrier`` (.1.1.1) returns ``Integer32`` in kHz
    # (e.g. ``5490000`` -> ``5490 MHz`` / ``5.49 GHz``). Stays in
    # integer kHz to avoid float drift; UI tier formats to MHz/GHz.
    # Optional.
    "frequency": "carrier_frequency_khz",
    # ``whispBoxActiveTxPower`` (.232.0) returns ``DisplayString`` like
    # ``"27 dBm"``. Optional.
    "transmitPower": "transmit_power_dbm",
}


class RadioMetricsReport(BaseModel):
    """Typed PMP 450i sector-level radio metrics report.

    Every field is a sector scalar from the ``whispBox*`` or
    ``whispApsRFConfigRadio*`` subtree; the per-LUID ``whispLinkEntry``
    tabular columns (``radioDownlinkRate``, ``radioUplinkRate``,
    ``signalStrengthRx``, ``ssr``, ``modulationMode``) are deliberately
    NOT modelled here because their ``.0`` instance does not exist on
    a physical PMP 450i AP. See issue #57.

    ``eirp_dbm`` is REQUIRED and always populated. The other four
    sector scalars are optional — they appear when the resolved
    ``OidCatalog`` carries them, and stay ``None`` otherwise. Keeping
    the model strict (``int`` / ``float``, not ``Optional[Any]``)
    preserves the no-dict / no-Any contract from Driver-R3.
    """

    model_config = ConfigDict(frozen=True)

    device_id: str
    fetched_at: datetime
    firmware: str
    eirp_dbm: int
    active_tx_power_dbm: float | None = None
    channel_bandwidth_mhz: float | None = None
    carrier_frequency_khz: int | None = None
    transmit_power_dbm: int | None = None

    @classmethod
    def fold(
        cls,
        *,
        device: Device,
        catalog: OidCatalog,
        values: dict[str, str | int],
        fetched_at: datetime,
    ) -> "RadioMetricsReport":
        """Fold raw OID->value pairs into a typed `RadioMetricsReport`.

        `values` is keyed by dotted OID. Missing REQUIRED OIDs (those in
        ``REQUIRED_OIDS``) raise ``NetworkUnreachableError`` (typed
        wire-level fault). Optional sector scalars (not in
        ``REQUIRED_OIDS``) are populated only when the catalog carries
        the OID AND the value is in the wire response; otherwise the
        field stays ``None``.
        """
        from nora.drivers.oid_catalog import REQUIRED_OIDS

        kwargs: dict[str, object] = {
            "device_id": device.device_id,
            "firmware": device.firmware,
            "fetched_at": fetched_at,
        }
        for oid_name, field_name in _OID_NAME_TO_FIELD.items():
            oid = catalog.oids.get(oid_name)
            if oid is None:
                # OID not in catalog: optional field stays None
                # unless the OID is in REQUIRED_OIDS (gate-enforced).
                if oid_name in REQUIRED_OIDS:
                    raise NetworkUnreachableError(f"{device.host}: missing required OID {oid_name}")
                continue
            raw = values.get(oid)
            if raw is None:
                if oid_name in REQUIRED_OIDS:
                    raise NetworkUnreachableError(
                        f"{device.host}: missing value for OID {oid} ({oid_name})"
                    )
                # Optional OID with no wire response: leave field None.
                continue
            kwargs[field_name] = _FIELD_PARSERS[field_name](raw, oid, device.host)
        return cls.model_validate(kwargs)


def _coerce_int(raw: str | int, oid: str, host: str) -> int:
    """Convert a raw SNMP scalar to int; raise `NetworkUnreachableError` on failure."""
    if isinstance(raw, int):
        return raw
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise NetworkUnreachableError(
            f"{host}: non-numeric SNMP value for {oid!r}: {raw!r}"
        ) from exc


def _coerce_dbm_hundredths(raw: str | int, oid: str, host: str) -> float:
    """Convert an Integer32 hundredths-of-dBm value to a float dBm.

    ``whispBoxActiveTxPowerInHundredthsDbm`` returns ``Integer32`` in
    hundredths of dBm (e.g. ``2700`` -> ``27.0 dBm``). Lossless against
    the wire integer; preserves 0.01 dBm precision.
    """
    as_int = _coerce_int(raw, oid, host)
    return as_int / 100.0


def _coerce_mhz_display(raw: str | int, oid: str, host: str) -> float:
    """Convert a DisplayString MHz value to float.

    ``channelBandwidth`` returns ``"20.0"`` (MHz, fractional).
    Tolerates bare integers (``"20"``) and decimal forms (``"20.0"``,
    ``"7.5"``).
    """
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip()
    try:
        return float(text)
    except (TypeError, ValueError) as exc:
        raise NetworkUnreachableError(
            f"{host}: non-parseable MHz DisplayString for {oid!r}: {raw!r}"
        ) from exc


def _coerce_dbm_display(raw: str | int, oid: str, host: str) -> int:
    """Parse a DisplayString dBm value to integer dBm.

    Cambium returns dBm values as a DisplayString like ``"44 dBm"``;
    some firmware revisions return ``"44"`` (bare integer) or
    ``"44.0 dBm"`` (fractional). The parser strips the unit and returns
    the integer dBm value. Non-parseable shapes surface as
    ``NetworkUnreachableError`.
    """
    if isinstance(raw, int):
        return raw
    text = str(raw).strip()
    # Strip a trailing unit token ("dBm", "dBmV", etc.) if present.
    if text.endswith("dBm"):
        text = text[: -len("dBm")].strip()
    elif text.endswith("dBmV"):
        text = text[: -len("dBmV")].strip()
    # Trim a trailing ".0" fractional suffix.
    if text.endswith(".0"):
        text = text[:-2]
    try:
        return int(float(text))
    except (TypeError, ValueError) as exc:
        raise NetworkUnreachableError(
            f"{host}: non-parseable dBm DisplayString for {oid!r}: {raw!r}"
        ) from exc


# Per-field parser dispatch. Keeps ``fold`` flat (no nested branching)
# and makes adding a new sector-scalar OID a one-line change. Each
# parser takes ``(raw, oid, host)`` and returns the typed scalar for
# the matching ``RadioMetricsReport`` field.
_FIELD_PARSERS: Final[dict[str, Callable[[str | int, str, str], int | float]]] = {
    "eirp_dbm": _coerce_dbm_display,
    "active_tx_power_dbm": _coerce_dbm_hundredths,
    "channel_bandwidth_mhz": _coerce_mhz_display,
    "carrier_frequency_khz": _coerce_int,
    "transmit_power_dbm": _coerce_dbm_display,
}


__all__ = ["RadioMetricsReport", "_OID_NAME_TO_FIELD"]
