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
from typing import Final

from pydantic import BaseModel, ConfigDict

from nora.drivers.exceptions import NetworkUnreachableError
from nora.drivers.inventory import Device
from nora.drivers.oid_catalog import OidCatalog

# Names of the fields in `RadioMetricsReport`. The mapping from OID
# catalog public name -> field name is one-to-one.
_OID_NAME_TO_FIELD: Final[dict[str, str]] = {
    "radioDownlinkRate": "radio_dl_rate_bps",
    "radioUplinkRate": "radio_ul_rate_bps",
    "signalStrengthRx": "rx_signal_dbm",
    "signalStrengthTx": "tx_signal_dbm",
    "ssr": "ssr",
    "modulationMode": "modulation",
}


class RadioMetricsReport(BaseModel):
    """Typed PMP 450i radio metrics report."""

    model_config = ConfigDict(frozen=True)

    device_id: str
    fetched_at: datetime
    firmware: str
    radio_dl_rate_bps: int
    radio_ul_rate_bps: int
    rx_signal_dbm: int
    tx_signal_dbm: int
    ssr: int
    modulation: str

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

        `values` is keyed by dotted OID. Missing required values raise
        `NetworkUnreachableError` (typed wire-level fault).
        """
        kwargs: dict[str, object] = {
            "device_id": device.device_id,
            "firmware": device.firmware,
            "fetched_at": fetched_at,
        }
        for oid_name, field_name in _OID_NAME_TO_FIELD.items():
            oid = catalog.oids.get(oid_name)
            if oid is None:
                # Should never happen — REQUIRED_OIDS ⊆ catalog.oids is
                # enforced at boot. Defensive: keep the contract anyway.
                raise NetworkUnreachableError(f"{device.host}: missing required OID {oid_name}")
            raw = values.get(oid)
            if raw is None:
                raise NetworkUnreachableError(
                    f"{device.host}: missing value for OID {oid} ({oid_name})"
                )
            if field_name == "modulation":
                kwargs[field_name] = str(raw)
            else:
                kwargs[field_name] = _coerce_int(raw, oid, device.host)
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


__all__ = ["RadioMetricsReport", "_OID_NAME_TO_FIELD"]
