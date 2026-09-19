"""Tests for the read-summary tools — PR 2 slice 2 of the PMP 450i
production surface (`2026-09-13-pmp450i-production-surface`).

These tests pin the public contract for the two read-summary
`@mcp.tool` registrations that land in PR 2:

* ``snmp_get_ap_summary`` — typed ``ApSummary`` (carrier, channel,
  tx_power, subscribers, sys_uptime).
* ``snmp_get_frame_utilization`` — typed ``FrameUtilization``
  (``dl_pct`` / ``ul_pct``).

Plus two contract tests for the read-path behaviour that PR 2 makes
visible to MCP clients:

* Unknown OID ⇒ ``logger.warning(...)`` AND the missing field is
  ``None`` (per `pmp450i-radio-tools` sub-cluster 1 scenario
  "unknown OID warns and returns None").
* Minor-mismatch fallback is exposed to the tool caller AND the
  literal telemetry line ``"OID catalog fallback: requested 15.3.1,
  using 15.3.0 (minor mismatch)"`` lands on stderr (per
  `oid-catalog` modified requirement "Minor Descending Fallback
  With Literal Warning" + scenario "minor mismatch returns closest
  lower minor with literal warning via the tool path").

Zero-Leakage: only TEST-NET-1 (``192.0.2.x``) host literals; no
real IPs, hostnames, serials, or credentials.

Named tests for PR 2:

* ``test_ap_summary_returns_typed_model``
* ``test_frame_utilization_returns_typed_model``
* ``test_unknown_oid_warn_and_value``
* ``test_catalog_minor_mismatch_warning_via_summary_call``
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
import yaml

from nora.drivers.inventory import Inventory
from nora.drivers.oid_catalog import OidCatalog, OidCatalogRegistry

# ---------------------------------------------------------------------------
# Helpers — inventory + catalog with both the radio-metrics and the
# summary OIDs (slice 2 adds three).
# ---------------------------------------------------------------------------


def _build_inventory(
    tmp_path: Path,
    *,
    ap_firmware: str = "15.2.1",
) -> Inventory:
    """Hermetic inventory with one v2c AP and one v3 SM."""
    payload = {
        "devices": [
            {
                "device_id": "ap-7400-01",
                "vendor": "cambium",
                "model": "pmp450i",
                "firmware": ap_firmware,
                "host": "192.0.2.10",
                "snmp_version": "v2c",
                "community": "change-me-v2c",
            },
            {
                "device_id": "sm-7400-02",
                "vendor": "cambium",
                "model": "pmp450i",
                "firmware": "15.2.1",
                "host": "192.0.2.11",
                "snmp_version": "v3",
                "auth_password": "change-me-auth",
                "priv_password": "change-me-priv",
            },
        ]
    }
    inv_path = tmp_path / "devices.yaml"
    inv_path.write_text(yaml.safe_dump(payload))
    return Inventory.from_yaml(inv_path)


def _summary_oid_map() -> dict[str, str]:
    """The four NEW summary OID names + dotted OIDs (synthesised public refs).

    The OID values are stable public object names — Cambium public docs
    (no MIB prose). The dotted OID format follows the existing Cambium
    private-enterprise branch (``1.3.6.1.4.1.161.19.3.x.x.0``) and the
    indices continue the existing pattern (channelBandwidth=7,
    frequency=8, transmitPower=9, receivePower=10, jitter=11,
    linkStatus=50, upTime=51). The summary branch lands at indices
    52-54 + 60 to avoid colliding with the existing seed.
    """
    return {
        "apFirmwareVersion": "1.3.6.1.4.1.161.19.3.1.1.52.0",
        "subscribersCount": "1.3.6.1.4.1.161.19.3.1.1.60.0",
        "frameUtilizationDlPct": "1.3.6.1.4.1.161.19.3.1.1.53.0",
        "frameUtilizationUlPct": "1.3.6.1.4.1.161.19.3.1.1.54.0",
    }


def _legacy_oid_map() -> dict[str, str]:
    """The v1 seed OIDs that ``summaries.py`` reuses for the AP summary.

    These names live in the v1 catalog JSON (radioDownlinkRate,
    radioUplinkRate, ... upTime) but are not part of the radio-metrics
    REQUIRED_OIDS set. ``summaries.py`` looks them up by name against
    the resolved catalog; the test fixture exposes them so the read
    tool can resolve every name it requests.
    """
    return {
        "channelBandwidth": "1.3.6.1.4.1.161.19.3.1.1.7.0",
        "frequency": "1.3.6.1.4.1.161.19.3.1.1.8.0",
        "transmitPower": "1.3.6.1.4.1.161.19.3.1.1.9.0",
        "upTime": "1.3.6.1.4.1.161.19.3.1.1.51.0",
    }


def _radio_seed_oids() -> dict[str, str]:
    """The six radio-metrics REQUIRED_OIDs.

    Mirror of ``_REQUIRED_OIDS_BY_VENDOR_MODEL[("cambium", "pmp450i")]``
    in the driver module — only the radio-metrics subset, used as the
    baseline for every test fixture.
    """
    return {
        "radioDownlinkRate": "1.3.6.1.4.1.161.19.3.1.1.1.0",
        "radioUplinkRate": "1.3.6.1.4.1.161.19.3.1.1.2.0",
        "signalStrengthRx": "1.3.6.1.4.1.161.19.3.1.1.3.0",
        # Issue #54 (2026-09-19): signalStrengthTx (broken
        # maxSMTxPwr) replaced by eirp (whispBoxActiveEIRP,
        # .306.0).
        "eirp": "1.3.6.1.4.1.161.19.3.1.1.4.0",
        "ssr": "1.3.6.1.4.1.161.19.3.1.1.5.0",
        "modulationMode": "1.3.6.1.4.1.161.19.3.1.1.6.0",
    }


def _build_catalog(
    firmware: str = "15.2.1",
    *,
    include_summary_oids: bool = True,
) -> OidCatalogRegistry:
    """Catalog registry carrying the radio seed + summary OIDs + legacy
    OIDs that ``summaries.py`` reuses.

    When ``include_summary_oids`` is ``False`` the catalog omits the
    four NEW summary OID names so the unknown-OID warning path can be
    exercised against a hermetic in-memory registry without touching
    disk state.
    """
    oids: dict[str, str] = {}
    oids.update(_radio_seed_oids())
    oids.update(_legacy_oid_map())
    if include_summary_oids:
        oids.update(_summary_oid_map())
    catalog = OidCatalog(
        vendor="cambium",
        model="pmp450i",
        firmware=firmware,
        oids=oids,
    )
    return OidCatalogRegistry(
        _catalogs_path=Path("."),
        _catalogs={("cambium", "pmp450i", firmware): catalog},
    )


def _build_two_firmware_registry(
    *,
    include_summary_oids: bool = True,
) -> OidCatalogRegistry:
    """Two-firmware registry: ``15.2.1`` + ``15.3.0``.

    The minor-mismatch warning test asserts that requesting ``15.3.1``
    against this registry falls back to ``15.3.0`` and emits the literal
    telemetry line.
    """
    oids: dict[str, str] = {}
    oids.update(_radio_seed_oids())
    oids.update(_legacy_oid_map())
    if include_summary_oids:
        oids.update(_summary_oid_map())

    catalog_a = OidCatalog(
        vendor="cambium",
        model="pmp450i",
        firmware="15.2.1",
        oids=dict(oids),
    )
    catalog_b = OidCatalog(
        vendor="cambium",
        model="pmp450i",
        firmware="15.3.0",
        oids=dict(oids),
    )
    return OidCatalogRegistry(
        _catalogs_path=Path("."),
        _catalogs={
            ("cambium", "pmp450i", "15.2.1"): catalog_a,
            ("cambium", "pmp450i", "15.3.0"): catalog_b,
        },
    )


class _FakeSnmpClient:
    """Fake client — returns canned values keyed by dotted OID.

    The summary tests use a hand-rolled dict rather than
    ``mock.MagicMock(spec=SnmpClient)`` so the OID lookup semantics
    (raw str / int return) match the production ``SnmpClient`` Protocol.
    """

    def __init__(self, values: dict[str, str | int]) -> None:
        self._values = dict(values)
        self.get_calls: list[str] = []

    def get_oid(self, oid: str) -> str | int:
        self.get_calls.append(oid)
        if oid not in self._values:
            raise KeyError(oid)
        return self._values[oid]

    def walk(self, base_oid: str) -> list[tuple[str, str | int]]:
        return []

    def close(self) -> None:
        return None


def _build_driver(
    *,
    inventory: Inventory,
    registry: OidCatalogRegistry,
    canned: _FakeSnmpClient,
) -> Any:
    """Construct a ``Pmp450iSnmpDriver`` wired to the canned fake client."""
    from nora.drivers.snmp_pmp450i import Pmp450iSnmpDriver

    return Pmp450iSnmpDriver(
        inventory=inventory,
        catalog_registry=registry,
        client_factory=lambda d: canned,
    )


# ---------------------------------------------------------------------------
# Named test #1 — ap_summary_returns_typed_model
# ---------------------------------------------------------------------------


def test_ap_summary_returns_typed_model(tmp_path: Path) -> None:
    """``snmp_get_ap_summary`` returns a typed ``ApSummary`` model.

    The model exposes the fields required by
    `pmp450i-radio-tools/spec.md` sub-cluster 1 scenario "ap_summary
    returns a typed model": carrier frequency, channel width, tx
    power, subscriber count, and system uptime. Each is a typed
    scalar — the model is JSON-serialisable without further coercion.
    """
    from nora.drivers.snmp_pmp450i.summaries import ApSummary

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1", include_summary_oids=True)
    summary_dotted = _summary_oid_map()
    canned = _FakeSnmpClient(
        {
            summary_dotted["apFirmwareVersion"]: "15.2.1",
            "1.3.6.1.4.1.161.19.3.1.1.8.0": "5800",  # frequency (MHz)
            "1.3.6.1.4.1.161.19.3.1.1.7.0": "20",  # channel width (MHz)
            "1.3.6.1.4.1.161.19.3.1.1.9.0": "23",  # transmit power (dBm)
            "1.3.6.1.4.1.161.19.3.1.1.60.0": "12",  # subscribers count
            "1.3.6.1.4.1.161.19.3.1.1.51.0": "86400",  # sys uptime seconds
        }
    )
    driver = _build_driver(inventory=inv, registry=registry, canned=canned)
    summary = driver.fetch_ap_summary("ap-7400-01")

    assert isinstance(summary, ApSummary), (
        f"fetch_ap_summary must return ApSummary; got {type(summary).__name__}"
    )
    dumped = summary.model_dump(mode="json")
    assert dumped["firmware"] == "15.2.1"
    assert dumped["carrier_frequency_mhz"] == 5800
    assert dumped["channel_width_mhz"] == 20
    assert dumped["tx_power_dbm"] == 23
    assert dumped["subscribers_count"] == 12
    assert dumped["sys_uptime_seconds"] == 86400


# ---------------------------------------------------------------------------
# Named test #2 — frame_utilization_returns_typed_model
# ---------------------------------------------------------------------------


def test_frame_utilization_returns_typed_model(tmp_path: Path) -> None:
    """``snmp_get_frame_utilization`` returns a typed ``FrameUtilization``.

    Per `pmp450i-radio-tools/spec.md` sub-cluster 1 scenario "frame
    utilization returns a typed model": downlink + uplink percentages
    are returned as typed floats.
    """
    from nora.drivers.snmp_pmp450i.summaries import FrameUtilization

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1", include_summary_oids=True)
    summary_dotted = _summary_oid_map()
    canned = _FakeSnmpClient(
        {
            summary_dotted["frameUtilizationDlPct"]: "42",
            summary_dotted["frameUtilizationUlPct"]: "31",
        }
    )
    driver = _build_driver(inventory=inv, registry=registry, canned=canned)
    utilization = driver.fetch_frame_utilization("ap-7400-01")

    assert isinstance(utilization, FrameUtilization), (
        f"fetch_frame_utilization must return FrameUtilization; got {type(utilization).__name__}"
    )
    dumped = utilization.model_dump(mode="json")
    assert dumped["dl_pct"] == 42.0
    assert dumped["ul_pct"] == 31.0


# ---------------------------------------------------------------------------
# Named test #3 — unknown_oid_warn_and_value
# ---------------------------------------------------------------------------


def test_unknown_oid_warn_and_value(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A catalog missing one AP-summary OID emits a warning AND
    returns ``None`` for the missing field.

    Per `pmp450i-radio-tools/spec.md` sub-cluster 1 scenario "unknown
    OID warns and returns None": when the catalog omits one
    AP-summary OID name, the driver still resolves the catalog
    successfully but the field-level fetch falls back to ``None`` with
    a literal ``"OID catalog fallback: ..."`` line on the telemetry
    channel.
    """
    from nora.drivers.snmp_pmp450i.summaries import ApSummary

    inv = _build_inventory(tmp_path)
    # Catalog missing `frequency` / `channelBandwidth` /
    # `transmitPower` / `subscribersCount` / `upTime` (intentionally).
    # The tool still resolves the catalog (the radio-metrics seed is
    # present + ``apFirmwareVersion`` is present) but every OTHER
    # AP-summary OID name lookup falls back to ``None`` with a
    # warning.
    oids: dict[str, str] = {
        # Radio seed.
        "radioDownlinkRate": "1.3.6.1.4.1.161.19.3.1.1.1.0",
        "radioUplinkRate": "1.3.6.1.4.1.161.19.3.1.1.2.0",
        "signalStrengthRx": "1.3.6.1.4.1.161.19.3.1.1.3.0",
        # Issue #54 (2026-09-19): signalStrengthTx (broken
        # maxSMTxPwr) replaced by eirp (whispBoxActiveEIRP,
        # .306.0).
        "eirp": "1.3.6.1.4.1.161.19.3.1.1.4.0",
        "ssr": "1.3.6.1.4.1.161.19.3.1.1.5.0",
        "modulationMode": "1.3.6.1.4.1.161.19.3.1.1.6.0",
        # Present — the only AP-summary OID name available.
        "apFirmwareVersion": "1.3.6.1.4.1.161.19.3.1.1.52.0",
        # Intentionally omit: frequency, channelBandwidth, transmitPower,
        # subscribersCount, upTime, frameUtilizationDlPct, frameUtilizationUlPct.
    }
    catalog = OidCatalog(
        vendor="cambium",
        model="pmp450i",
        firmware="15.2.1",
        oids=oids,
    )
    registry = OidCatalogRegistry(
        _catalogs_path=Path("."),
        _catalogs={("cambium", "pmp450i", "15.2.1"): catalog},
    )
    canned = _FakeSnmpClient(
        {
            "1.3.6.1.4.1.161.19.3.1.1.52.0": "15.2.1",
            # Wire values present even for missing OID names — the
            # tool must NOT consult them.
            "1.3.6.1.4.1.161.19.3.1.1.8.0": "5800",
        }
    )
    driver = _build_driver(inventory=inv, registry=registry, canned=canned)

    with caplog.at_level(logging.WARNING, logger="nora.drivers.snmp_pmp450i.summaries"):
        summary = driver.fetch_ap_summary("ap-7400-01")

    assert isinstance(summary, ApSummary)
    dumped = summary.model_dump(mode="json")
    # The present OID round-trips.
    assert dumped.get("firmware") == "15.2.1"
    # Every missing OID name yields ``None`` — never an exception.
    assert dumped["carrier_frequency_mhz"] is None
    assert dumped["channel_width_mhz"] is None
    assert dumped["tx_power_dbm"] is None
    assert dumped["subscribers_count"] is None
    assert dumped["sys_uptime_seconds"] is None

    warning_lines = [
        record.getMessage() for record in caplog.records if record.levelno == logging.WARNING
    ]
    assert any("OID catalog fallback" in line for line in warning_lines), (
        f"Expected a literal 'OID catalog fallback' warning; got {warning_lines!r}"
    )


# ---------------------------------------------------------------------------
# Named test #4 — catalog_minor_mismatch_warning_via_summary_call
# ---------------------------------------------------------------------------


def test_catalog_minor_mismatch_warning_via_summary_call(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Minor-mismatch fallback is exposed through ``snmp_get_ap_summary``.

    Per `oid-catalog/spec.md` MODIFIED scenario "minor mismatch
    returns closest lower minor with literal warning via the tool
    path": the registry holds ``15.2.1`` and ``15.3.0``; the device
    advertises ``15.3.1``; the tool picks ``15.3.0`` AND stderr carries
    the literal ``"OID catalog fallback: requested 15.3.1, using
    15.3.0 (minor mismatch)"`` line.
    """
    from nora.drivers.snmp_pmp450i.summaries import ApSummary

    # Inventory advertises 15.3.1 — the registry falls back to the
    # closest lower minor (15.3.0).
    inv = _build_inventory(tmp_path, ap_firmware="15.3.1")
    registry = _build_two_firmware_registry(include_summary_oids=True)

    summary_dotted = _summary_oid_map()
    canned = _FakeSnmpClient(
        {
            summary_dotted["apFirmwareVersion"]: "15.3.1",
            "1.3.6.1.4.1.161.19.3.1.1.8.0": "5800",
            "1.3.6.1.4.1.161.19.3.1.1.7.0": "20",
            "1.3.6.1.4.1.161.19.3.1.1.9.0": "23",
            "1.3.6.1.4.1.161.19.3.1.1.60.0": "12",
            "1.3.6.1.4.1.161.19.3.1.1.51.0": "86400",
        }
    )
    driver = _build_driver(inventory=inv, registry=registry, canned=canned)

    with caplog.at_level(logging.WARNING, logger="nora.drivers.oid_catalog"):
        summary = driver.fetch_ap_summary("ap-7400-01")

    assert isinstance(summary, ApSummary)
    dumped = summary.model_dump(mode="json")
    assert dumped["carrier_frequency_mhz"] == 5800

    warning_lines = [
        record.getMessage() for record in caplog.records if record.levelno == logging.WARNING
    ]
    expected = "OID catalog fallback: requested 15.3.1, using 15.3.0 (minor mismatch)"
    assert any(expected in line for line in warning_lines), (
        f"Expected literal {expected!r} warning; got {warning_lines!r}"
    )
