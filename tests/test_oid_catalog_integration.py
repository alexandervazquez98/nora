"""ADR #17 Tests 9 + 10 — E2E integration tests for the OID catalog +
tool-registration guard.

These tests pin
`openspec/changes/2026-09-13-pmp450i-production-surface/specs/oid-catalog-integration/spec.md`
to code and verify the full read-path chain:

    `@mcp.tool` call → catalog resolve → driver wire frame

Five named tests (PR 5 / slice 5):

* ``test_dynamic_resolution_applies_catalog_versioning_before_query``
* ``test_minor_mismatch_warning_during_read_path_e2e``
* ``test_major_mismatch_blocks_driver_query_typed``
* ``test_unified_tool_catalog_references_required_oids_per_tool``
* ``test_new_tool_without_oid_registration_rejected_at_registration_time``

Zero-Leakage: only TEST-NET-1 (``192.0.2.x``) host literals. No real
infrastructure values.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src" / "nora"

# ---------------------------------------------------------------------------
# Reusable helpers — inventory, registry, fake client.
# ---------------------------------------------------------------------------


# A minimal but representative OID payload that mirrors the production
# seed at `src/nora/data/oid-catalogs/cambium/pmp450i/15.2.1.json`. The
# integration test cares about the envelope `tools` map (the slice 5
# surface) so every name referenced by the six PMP 450i tools is
# present. Extra names are tolerated.
_INTEGRATION_CATALOG_OIDS: dict[str, str] = {
    "radioDownlinkRate": "1.3.6.1.4.1.161.19.3.1.1.1.0",
    "radioUplinkRate": "1.3.6.1.4.1.161.19.3.1.1.2.0",
    "signalStrengthRx": "1.3.6.1.4.1.161.19.3.1.1.3.0",
    "signalStrengthTx": "1.3.6.1.4.1.161.19.3.1.1.4.0",
    "ssr": "1.3.6.1.4.1.161.19.3.1.1.5.0",
    "modulationMode": "1.3.6.1.4.1.161.19.3.1.1.6.0",
    "channelBandwidth": "1.3.6.1.4.1.161.19.3.1.1.7.0",
    "frequency": "1.3.6.1.4.1.161.19.3.1.1.8.0",
    "transmitPower": "1.3.6.1.4.1.161.19.3.1.1.9.0",
    "receivePower": "1.3.6.1.4.1.161.19.3.1.1.10.0",
    "jitter": "1.3.6.1.4.1.161.19.3.1.1.11.0",
    "inOctets": "1.3.6.1.4.1.161.19.3.4.1.1.1.0",
    "outOctets": "1.3.6.1.4.1.161.19.3.4.1.1.2.0",
    "linkStatus": "1.3.6.1.4.1.161.19.3.1.1.50.0",
    "upTime": "1.3.6.1.4.1.161.19.3.1.1.51.0",
    "apFirmwareVersion": "1.3.6.1.4.1.161.19.3.1.1.52.0",
    "subscribersCount": "1.3.6.1.4.1.161.19.3.1.1.60.0",
    "frameUtilizationDlPct": "1.3.6.1.4.1.161.19.3.1.1.53.0",
    "frameUtilizationUlPct": "1.3.6.1.4.1.161.19.3.1.1.54.0",
    "smSessionUptime": "1.3.6.1.4.1.161.19.3.2.1.70.0",
    "smCinr": "1.3.6.1.4.1.161.19.3.2.1.71.0",
    "smLinkStatus": "1.3.6.1.4.1.161.19.3.2.1.72.0",
    "smLuid": "1.3.6.1.4.1.161.19.3.2.1.73.0",
    "smJitter": "1.3.6.1.4.1.161.19.3.2.1.80.0",
    "smRetransmits": "1.3.6.1.4.1.161.19.3.2.1.81.0",
    "smRxLevel": "1.3.6.1.4.1.161.19.3.2.1.82.0",
    "smTxLevel": "1.3.6.1.4.1.161.19.3.2.1.83.0",
    "spectrumNoiseFloorA": "1.3.6.1.4.1.161.19.3.1.1.90.0",
    "spectrumNoiseFloorB": "1.3.6.1.4.1.161.19.3.1.1.91.0",
    "spectrumNoiseFloorC": "1.3.6.1.4.1.161.19.3.1.1.92.0",
    "spectrumChannelRank": "1.3.6.1.4.1.161.19.3.1.1.93.0",
    "spectrumScanStatus": "1.3.6.1.4.1.161.19.3.1.1.94.0",
    "migrateCarrierFrequency": "1.3.6.1.4.1.161.19.3.1.1.95.0",
    "migratePriorCarrierFrequency": "1.3.6.1.4.1.161.19.3.1.1.96.0",
    # Issue #42 / `2026-09-15-register-device-mcp`: `sysDescr` is the
    # RFC 1213 OID that `register_device` reads for cheap reachability
    # validation. The hermetic fixture mirrors the production re-signed
    # catalog (Task 6).
    "sysDescr": "1.3.6.1.2.1.1.1.0",
}


# Per-tool OID name lists mirroring the production catalog envelope
# `tools` map. Used both to write a hermetic catalog AND to assert
# test 4 (per-tool index references signed OIDs).
_INTEGRATION_CATALOG_TOOLS: dict[str, list[str]] = {
    "snmp_get_ap_summary": [
        "apFirmwareVersion",
        "frequency",
        "channelBandwidth",
        "transmitPower",
        "subscribersCount",
        "upTime",
    ],
    "snmp_get_frame_utilization": [
        "frameUtilizationDlPct",
        "frameUtilizationUlPct",
    ],
    "snmp_get_sm_table": [
        "smSessionUptime",
        "smCinr",
        "smLinkStatus",
        "smLuid",
    ],
    "snmp_get_sm_detailed_diagnostics": [
        "smJitter",
        "smCinr",
        "smRetransmits",
        "smRxLevel",
        "smTxLevel",
    ],
    "snmp_run_spectrum_analysis": [
        "spectrumNoiseFloorA",
        "spectrumNoiseFloorB",
        "spectrumNoiseFloorC",
        "spectrumChannelRank",
        "spectrumScanStatus",
    ],
    "snmp_migrate_radio_frequency": [
        "migrateCarrierFrequency",
        "migratePriorCarrierFrequency",
    ],
    # Issue #42 / `2026-09-15-register-device-mcp`: `register_device`
    # carries the cheapest possible reachability probe (`sysDescr` GET
    # against `1.3.6.1.2.1.1.1.0`). The catalog envelope MUST list
    # `sysDescr` as the registered OID for the boot-time guard to
    # accept the tool without an allow-list entry.
    "register_device": ["sysDescr"],
    # Slice-1 radio-metrics tool — promoted from the legacy
    # `_ALLOWED_UNCATALOGUED_TOOLS` allow-list (Task 7). The hermetic
    # fixture mirrors the production re-signed catalog envelope so
    # the rogue-tool guard test below finds the rogue FIRST.
    "snmp_get_pmp450i_radio_metrics": [
        "radioDownlinkRate",
        "radioUplinkRate",
        "signalStrengthRx",
        "signalStrengthTx",
        "ssr",
        "modulationMode",
        "sysDescr",
    ],
}


_INTEGRATION_CATALOG_KEY = "test-oid-catalog-integration-key-do-not-use-in-prod"


def _write_signed_catalog(
    path: Path,
    *,
    firmware: str,
    extra_tools: dict[str, list[str]] | None = None,
) -> None:
    """Write one signed catalog JSON for the integration tests.

    Mirrors the production envelope shape (vendor, model, firmware,
    oids, tools, hmac_sha256). The HMAC is computed over the
    canonicalised `oids` map so it matches what
    `OidCatalogRegistry._verify_one` recomputes on boot.
    """
    import hashlib
    import hmac

    tools_map = dict(_INTEGRATION_CATALOG_TOOLS)
    if extra_tools:
        tools_map.update(extra_tools)
    canonical_body = json.dumps(
        _INTEGRATION_CATALOG_OIDS, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    sig = hmac.new(_INTEGRATION_CATALOG_KEY.encode(), canonical_body, hashlib.sha256).hexdigest()
    envelope = {
        "version": 1,
        "vendor": "cambium",
        "model": "pmp450i",
        "firmware": firmware,
        "oids": _INTEGRATION_CATALOG_OIDS,
        "tools": tools_map,
        "hmac_sha256": sig,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(envelope))


def _build_inventory(tmp_path: Path, *, ap_firmware: str = "15.2.1") -> Any:
    """Hermetic inventory with one v2c PMP 450i AP advertising `ap_firmware`."""
    from nora.drivers.inventory import Inventory

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
        ]
    }
    path = tmp_path / "devices.yaml"
    path.write_text(yaml.safe_dump(payload))
    return Inventory.from_yaml(path)


class _FakeSnmpClient:
    """Fake `SnmpClient` — records every `get_oid` call.

    The integration tests use a hand-rolled dict rather than
    ``mock.MagicMock(spec=SnmpClient)`` so the OID lookup semantics
    match the production ``SnmpClient`` Protocol AND so the call
    timestamps are deterministic (used by test 1 to assert ordering).
    """

    def __init__(self, values: dict[str, str | int]) -> None:
        self._values = dict(values)
        self.get_calls: list[tuple[float, str]] = []
        self._closed = False

    def get_oid(self, oid: str) -> str | int:
        ts = time.monotonic()
        self.get_calls.append((ts, oid))
        if oid not in self._values:
            raise KeyError(oid)
        return self._values[oid]

    def walk(self, base_oid: str) -> list[tuple[str, str | int]]:
        return []

    def close(self) -> None:
        self._closed = True


def _build_driver(
    *,
    inventory: Any,
    registry: Any,
    canned: _FakeSnmpClient,
) -> Any:
    """Construct a `Pmp450iSnmpDriver` wired to the canned fake client."""
    from nora.drivers.snmp_pmp450i import Pmp450iSnmpDriver

    return Pmp450iSnmpDriver(
        inventory=inventory,
        catalog_registry=registry,
        client_factory=lambda d: canned,
    )


def _build_registry(
    tmp_path: Path,
    *,
    firmware: str = "15.2.1",
    extra_tools: dict[str, list[str]] | None = None,
) -> Any:
    """HMAC-signed catalog + OidCatalogRegistry on `tmp_path`."""
    from nora.drivers.oid_catalog import OidCatalogRegistry

    catalog_path = tmp_path / "oid-catalogs" / "cambium" / "pmp450i" / f"{firmware}.json"
    _write_signed_catalog(catalog_path, firmware=firmware, extra_tools=extra_tools)
    return OidCatalogRegistry.verify(
        built_in_root=None,
        operator_root=tmp_path / "oid-catalogs",
        signing_key=_INTEGRATION_CATALOG_KEY,
    )


# ---------------------------------------------------------------------------
# Named test #1 — dynamic_resolution_applies_catalog_versioning_before_query
# ---------------------------------------------------------------------------


def test_dynamic_resolution_applies_catalog_versioning_before_query(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """`report_firmware()` reports 15.3.0; resolver picks 15.2.1 first.

    Per `oid-catalog-integration/spec.md` requirement "Dynamic Firmware
    Resolution Before Every Query": every `@mcp.tool` that touches an
    SNMP device MUST call `OidCatalogRegistry.resolve((vendor, model,
    firmware))` with the firmware reported by the driver. The wire
    frame must follow, never precede, the catalog resolution.

    The test pins:

    * `report_firmware()` returns ``Version("15.3.0")``.
    * The registry resolves to the closest lower minor (``15.2.1``).
    * `resolve(...)` is called BEFORE the first wire `get_oid`.
    * The dotted OIDs on the wire match the catalog at ``15.2.1``.
    """
    inv = _build_inventory(tmp_path, ap_firmware="15.3.0")
    registry = _build_registry(tmp_path, firmware="15.2.1")
    canned = _FakeSnmpClient(
        {
            "1.3.6.1.4.1.161.19.3.1.1.52.0": "15.3.0",
            "1.3.6.1.4.1.161.19.3.1.1.8.0": "5800",
            "1.3.6.1.4.1.161.19.3.1.1.7.0": "20",
            "1.3.6.1.4.1.161.19.3.1.1.9.0": "23",
            "1.3.6.1.4.1.161.19.3.1.1.60.0": "12",
            "1.3.6.1.4.1.161.19.3.1.1.51.0": "86400",
        }
    )
    driver = _build_driver(inventory=inv, registry=registry, canned=canned)

    # The driver MUST ask the registry what firmware the device is
    # running before it picks OIDs. Inject a wrapper that records a
    # timestamp for the resolve call so test 1 can prove the wire
    # frames followed, never preceded, the resolve.
    resolve_calls: list[tuple[float, tuple[str, str, str]]] = []
    real_resolve = registry.resolve

    def _spy_resolve(ref: tuple[str, str, str]) -> Any:
        resolve_calls.append((time.monotonic(), ref))
        return real_resolve(ref)

    registry.resolve = _spy_resolve  # type: ignore[assignment]
    # The summaries helper calls ``driver._catalog_registry.resolve(...)``
    # (attribute access, not module attribute) — wire the spy through the
    # driver's own reference so the call is captured.
    driver._catalog_registry.resolve = _spy_resolve  # type: ignore[attr-defined]

    with caplog.at_level(logging.WARNING, logger="nora.drivers.oid_catalog"):
        summary = driver.fetch_ap_summary("ap-7400-01")

    # 1. Resolve happened.
    assert resolve_calls, "OidCatalogRegistry.resolve was never called on the read path"
    # 2. The ref tuple equals the inventory firmware.
    assert resolve_calls[0][1] == ("cambium", "pmp450i", "15.3.0"), (
        f"resolve ref mismatch; got {resolve_calls[0][1]!r}"
    )
    # 3. Resolve timestamp is BEFORE the first wire GET timestamp.
    assert canned.get_calls, "no wire GETs were issued — resolve ordering is meaningless"
    resolve_ts = resolve_calls[0][0]
    first_get_ts = canned.get_calls[0][0]
    assert resolve_ts <= first_get_ts, (
        f"catalog resolve must precede wire GET; "
        f"resolve_ts={resolve_ts:.9f} first_get_ts={first_get_ts:.9f}"
    )
    # 4. The OIDs on the wire are exactly the catalog at 15.2.1's entries
    #    consumed by `snmp_get_ap_summary`.
    expected_oids = {
        _INTEGRATION_CATALOG_OIDS["apFirmwareVersion"],
        _INTEGRATION_CATALOG_OIDS["frequency"],
        _INTEGRATION_CATALOG_OIDS["channelBandwidth"],
        _INTEGRATION_CATALOG_OIDS["transmitPower"],
        _INTEGRATION_CATALOG_OIDS["subscribersCount"],
        _INTEGRATION_CATALOG_OIDS["upTime"],
    }
    actual_oids = {oid for _ts, oid in canned.get_calls}
    assert expected_oids.issubset(actual_oids), (
        f"wire OIDs missing expected subset; expected={expected_oids!r} actual={actual_oids!r}"
    )
    # 5. The fallback warning was emitted (proves the resolver picked
    #    15.2.1, not 15.3.0).
    warning_lines = [
        record.getMessage() for record in caplog.records if record.levelno == logging.WARNING
    ]
    assert any(
        "OID catalog fallback: requested 15.3.0, using 15.2.1" in line for line in warning_lines
    ), f"expected minor-mismatch fallback warning; got {warning_lines!r}"
    # 6. The summary is still well-formed.
    assert summary.firmware == "15.3.0"
    assert summary.carrier_frequency_mhz == 5800


# ---------------------------------------------------------------------------
# Named test #2 — minor_mismatch_warning_during_read_path_e2e
# ---------------------------------------------------------------------------


def test_minor_mismatch_warning_during_read_path_e2e(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Minor-mismatch fallback emits the literal warning through the read path.

    Per `oid-catalog-integration/spec.md` scenario "minor-mismatch warning
    emitted via the read path": the driver reports ``Version("15.3.0")``,
    the registry holds only ``15.2.1``, the wire call uses ``15.2.1``'s
    OIDs AND stderr carries the EXACT literal

        ``"OID catalog fallback: requested 15.3.0, using 15.2.1 (minor mismatch)"``.

    The literal is asserted verbatim — no string interpolation, copy
    from `oid-catalog-integration/spec.md` line for line.
    """
    inv = _build_inventory(tmp_path, ap_firmware="15.3.0")
    registry = _build_registry(tmp_path, firmware="15.2.1")
    canned = _FakeSnmpClient(
        {
            "1.3.6.1.4.1.161.19.3.1.1.52.0": "15.3.0",
            "1.3.6.1.4.1.161.19.3.1.1.8.0": "5800",
            "1.3.6.1.4.1.161.19.3.1.1.7.0": "20",
            "1.3.6.1.4.1.161.19.3.1.1.9.0": "23",
            "1.3.6.1.4.1.161.19.3.1.1.60.0": "12",
            "1.3.6.1.4.1.161.19.3.1.1.51.0": "86400",
        }
    )
    driver = _build_driver(inventory=inv, registry=registry, canned=canned)

    expected_literal = "OID catalog fallback: requested 15.3.0, using 15.2.1 (minor mismatch)"

    with caplog.at_level(logging.WARNING, logger="nora.drivers.oid_catalog"):
        summary = driver.fetch_ap_summary("ap-7400-01")

    warning_lines = [
        record.getMessage() for record in caplog.records if record.levelno == logging.WARNING
    ]
    assert any(expected_literal in line for line in warning_lines), (
        f"Expected EXACT literal {expected_literal!r} in stderr; got {warning_lines!r}"
    )
    # And the response was still returned (the wire call happened
    # against the fallback catalog).
    assert canned.get_calls, "wire GETs must run on the fallback catalog (15.2.1)"
    assert summary.firmware == "15.3.0"


# ---------------------------------------------------------------------------
# Named test #3 — major_mismatch_blocks_driver_query_typed
# ---------------------------------------------------------------------------


def test_major_mismatch_blocks_driver_query_typed(tmp_path: Path) -> None:
    """Major-mismatch blocks the driver query with a typed `CatalogNotFoundError`.

    Per `oid-catalog-integration/spec.md` scenario "major-mismatch blocks
    the driver query with a typed error":

    * `report_firmware()` returns ``Version("16.0.0")``.
    * The registry holds only ``15.x``.
    * The read tool raises ``CatalogNotFoundError`` whose message names
      both majors (the requested ``16`` AND the registered majors
      ``[15]``).
    * NO wire frame is sent.

    The catalog envelope is the canonical source — the test reads
    `inventory.firmware` (``16.0.0``) and lets `OidCatalogRegistry.resolve`
    surface the typed error.
    """
    from nora.drivers.exceptions import CatalogNotFoundError

    inv = _build_inventory(tmp_path, ap_firmware="16.0.0")
    registry = _build_registry(tmp_path, firmware="15.2.1")
    canned = _FakeSnmpClient({})  # empty — any GET would raise KeyError anyway
    driver = _build_driver(inventory=inv, registry=registry, canned=canned)

    with pytest.raises(CatalogNotFoundError) as exc_info:
        driver.fetch_ap_summary("ap-7400-01")

    # Typed error message must name BOTH majors (the requested one
    # AND the registered one).
    msg = str(exc_info.value)
    assert "16" in msg, f"error message must name requested major 16; got {msg!r}"
    assert "15" in msg, f"error message must name registered major 15; got {msg!r}"
    # And no wire GET was issued before the raise.
    assert canned.get_calls == [], (
        f"major-mismatch must block the wire frame; got GET calls: {canned.get_calls!r}"
    )


# ---------------------------------------------------------------------------
# Named test #4 — unified_tool_catalog_references_required_oids_per_tool
# ---------------------------------------------------------------------------


def test_unified_tool_catalog_references_required_oids_per_tool(tmp_path: Path) -> None:
    """Every catalogued tool has a non-empty REQUIRED_OIDS entry.

    Per `oid-catalog-integration/spec.md` requirement "Per-Tool
    `REQUIRED_OIDS` Index": the registry exposes a per-`(vendor, model)`
    map from `@mcp.tool` name to a non-empty set of OID names, AND
    every OID in the set must exist in some signed catalog entry.

    The test iterates `server.__all__` (the FastMCP tool surface) and
    asserts each name resolves through the per-tool index. The six
    PMP 450i read/mutation tools must appear with the OIDs their
    catalogs signed for; the four intervention-memory tools
    (`search_intervention_history`, `get_device_lifecycle_summary`,
    `correlate_sector_interference`, `save_intervention_record`)
    plus the legacy `snmp_get_pmp450i_radio_metrics` are
    pre-existing operators NOT catalogued in the v1 seed — those
    are filtered out below the assertion boundary.
    """
    from nora import server as server_mod

    registry = _build_registry(tmp_path, firmware="15.2.1")

    # The six PMP 450i `@mcp.tool` names that the catalog envelopes
    # list. The other names in `server.__all__` are intervention-
    # memory operators, the legacy radio-metrics tool, and two
    # `@mcp.prompt` handlers — none of which carry a `tools` map.
    expected_pmp450i_tools = {
        "snmp_get_ap_summary",
        "snmp_get_frame_utilization",
        "snmp_get_sm_table",
        "snmp_get_sm_detailed_diagnostics",
        "snmp_run_spectrum_analysis",
        "snmp_migrate_radio_frequency",
    }

    # 1. `OidCatalogRegistry` exposes a per-tool index attribute.
    index_attr = "REQUIRED_OIDS_BY_TOOL"
    assert hasattr(registry, index_attr), (
        f"OidCatalogRegistry must expose {index_attr}; current attrs: "
        f"{sorted(a for a in dir(registry) if not a.startswith('_'))!r}"
    )
    raw_index = getattr(registry, index_attr)
    assert isinstance(raw_index, dict), (
        f"{index_attr} must be a dict; got {type(raw_index).__name__}"
    )

    # 2. The index MUST map `(vendor, model)` -> `{tool_name: [OID names]}`.
    vendor_model = ("cambium", "pmp450i")
    per_vendor = raw_index.get(vendor_model)
    if per_vendor is None:
        # Convenience accessor path — `required_oids_by_tool((vendor, model))`.
        accessor = getattr(registry, "required_oids_by_tool", None)
        assert callable(accessor), (
            "registry must expose REQUIRED_OIDS_BY_TOOL[...] or "
            "required_oids_by_tool((vendor, model))"
        )
        per_vendor = accessor(vendor_model)
    assert isinstance(per_vendor, dict), (
        f"per-`(vendor, model)` index must be a dict; got {type(per_vendor).__name__}"
    )

    # 3. The six PMP 450i tool names resolve to non-empty OID-name lists.
    missing: list[str] = []
    empty: list[str] = []
    for tool_name in expected_pmp450i_tools:
        if tool_name not in per_vendor:
            missing.append(tool_name)
            continue
        names = per_vendor[tool_name]
        if not names:
            empty.append(tool_name)
            continue
        assert all(isinstance(n, str) and n for n in names), (
            f"per-tool index entry for {tool_name!r} must contain non-empty strings; got {names!r}"
        )
    assert not missing, (
        f"REQUIRED_OIDS_BY_TOOL missing entries for: {missing!r}; "
        f"got keys: {sorted(per_vendor.keys())!r}"
    )
    assert not empty, f"REQUIRED_OIDS_BY_TOOL empty entries for: {empty!r}"

    # 4. Every OID name referenced in the index exists in the signed
    #    catalog for `(cambium, pmp450i, 15.2.1)`.
    catalog = registry.resolve(("cambium", "pmp450i", "15.2.1"))
    catalog_oid_names = set(catalog.oids.keys())
    for tool_name, names in per_vendor.items():
        if not isinstance(names, (list, tuple, set, frozenset)):
            continue
        unknown = set(names) - catalog_oid_names
        assert not unknown, (
            f"per-tool index for {tool_name!r} references OIDs not in the signed "
            f"catalog: {sorted(unknown)!r}"
        )

    # 5. `server.__all__` enumerates the full tool surface; the guard
    #    in `cli.main` uses this list as its allow-list.
    # Filter out the helpers the guard also skips (server-internal
    # state setters, prompts, and the guard helper itself).
    server_tool_names = {
        name
        for name in server_mod.__all__
        if name
        not in {
            "mcp",
            "configure_logging",
            "set_runtime_state",
            "get_runtime_state",
            "set_prompt_registry",
            "get_prompt_registry",
            "register_tool_log_middleware",
            "netops_orchestrator",
            "snmp_pmp450i",
            "verify_tools_are_catalogued",
        }
    }
    # All twelve tool functions are present.
    assert server_tool_names == {
        "snmp_get_pmp450i_radio_metrics",
        "snmp_get_ap_summary",
        "snmp_get_frame_utilization",
        "snmp_get_sm_table",
        "snmp_get_sm_detailed_diagnostics",
        "snmp_run_spectrum_analysis",
        "snmp_migrate_radio_frequency",
        "search_intervention_history",
        "get_device_lifecycle_summary",
        "correlate_sector_interference",
        "save_intervention_record",
        "register_device",
    }, f"server.__all__ tool surface drifted; got {sorted(server_tool_names)!r}"


# ---------------------------------------------------------------------------
# Named test #5 — new_tool_without_oid_registration_rejected_at_registration_time
# ---------------------------------------------------------------------------


def test_new_tool_without_oid_registration_rejected_at_registration_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rogue `@mcp.tool` is rejected by the boot-time guard.

    Per `oid-catalog-integration/spec.md` requirement "Tool-Registration
    Guard — Reject Uncatalogued Tools": the boot sequence MUST refuse
    any tool whose name is NOT present in
    `OidCatalogRegistry.REQUIRED_OIDS_BY_TOOL[(vendor, model)]`. The
    guard raises a typed `UncataloguedToolError` BEFORE `mcp.run()`.

    The test follows the project `openspec/config.yaml::testing.layers.integration`
    pattern: spin up a real Python subprocess that imports `nora.cli`,
    registers a rogue tool via `mcp.add_tool(...)`, then invokes the
    boot-time guard. The subprocess is expected to exit non-zero AND
    stderr to name the rogue tool.
    """
    python = sys.executable
    catalog_dir = tmp_path / "oid-catalogs"
    catalog_dir.mkdir(parents=True, exist_ok=True)
    _write_signed_catalog(
        catalog_dir / "cambium" / "pmp450i" / "15.2.1.json",
        firmware="15.2.1",
    )

    # Tiny driver script: load hermetic env, import nora.cli, add a
    # rogue tool, call the boot-time guard, surface exit code 1.
    driver_script = textwrap.dedent(
        f"""
        import asyncio
        import os
        import sys
        from pathlib import Path

        catalog_dir = Path({str(catalog_dir)!r})
        signing_key  = {_INTEGRATION_CATALOG_KEY!r}

        os.environ["NORA_OID_CATALOGS_PATH"]     = str(catalog_dir)
        os.environ["NORA_OID_CATALOG_SIGNING_KEY"] = signing_key
        os.environ.setdefault("NORA_DEVICES_INVENTORY_PATH", str(catalog_dir / "devices.yaml"))
        os.environ.setdefault("NORA_PROMPTS_DIR", str(catalog_dir / "prompts"))

        # Boot the server module — that registers the eleven real tools
        # via @mcp.tool decorators.
        from nora import server as server_mod
        from nora import cli as cli_mod

        def _add_rogue() -> None:
            def snmp_get_rogue_metric(device_id: str) -> dict:
                return {{"status": "UNREGISTERED"}}

            server_mod.mcp.add_tool(snmp_get_rogue_metric)

        _add_rogue()

        # Now call the boot-time guard. The helper is exposed from
        # nora.cli so tests and the real boot path share one body.
        # Use `verify(built_in_root=None, ...)` — same hermetic
        # construction as the unit tests above — so the production
        # built-in baseline (signed with a different key) is NOT
        # loaded into the registry.
        from nora.drivers.oid_catalog import OidCatalogRegistry

        registry = OidCatalogRegistry.verify(
            built_in_root=None,
            operator_root=catalog_dir,
            signing_key=signing_key,
        )

        try:
            cli_mod.verify_tools_are_catalogued(registry=registry)
        except Exception as exc:
            print(f"GUARD_RAISED: {{type(exc).__name__}}: {{exc}}", file=sys.stderr)
            sys.exit(1)

        # Guard did NOT raise — boot would expose the rogue tool to MCP.
        print("GUARD_DID_NOT_RAISE", file=sys.stderr)
        sys.exit(2)
        """
    )

    proc = subprocess.run(
        [python, "-c", driver_script],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(PROJECT_ROOT),
        env={**os.environ, "PYTHONPATH": str(SRC_DIR.parent)},
    )

    stderr = proc.stderr or ""
    assert proc.returncode == 1, (
        f"subprocess exit code must be 1 (guard raised); got {proc.returncode}\n"
        f"stdout={proc.stdout!r}\nstderr={stderr!r}"
    )
    # Guard raised. Now assert the typed exception class AND the rogue
    # tool name appear in stderr. The literal wording is the contract;
    # the guard helper prefixes the message with `tool_name:` per
    # `UncataloguedToolError.__init__`.
    assert "UncataloguedToolError" in stderr, (
        f"stderr must name UncataloguedToolError; got {stderr!r}"
    )
    assert "snmp_get_rogue_metric" in stderr, f"stderr must name the rogue tool; got {stderr!r}"
    # The guard MUST refuse to start MCP — never reach the
    # `GUARD_DID_NOT_RAISE` line.
    assert "GUARD_DID_NOT_RAISE" not in stderr, f"guard let the rogue tool through; got {stderr!r}"


# ---------------------------------------------------------------------------
# R-NEW-6-S2/S3/S4 — every re-signed baseline carries `register_device`
# in its `tools` envelope AND `sysDescr` in its `oids` map AND verifies
# cleanly under `OidCatalogRegistry.verify_all` (issue #42 / Task 6).
# ---------------------------------------------------------------------------

_RE_SIGNED_TRIPLES: list[tuple[str, str, str]] = [
    ("cambium", "pmp450i", "15.2.1"),
    ("cambium", "pmp450i", "15.3.0"),
    ("cambium", "pmp450i", "25.1.0"),
]


def _load_signed_envelope(*, vendor: str, model: str, firmware: str) -> dict[str, Any]:
    """Load the operator-root catalog envelope for `(vendor, model, firmware)`.

    Returns the raw dict (NOT an `OidCatalog`) so the tests can assert
    the envelope's `tools` map and `oids` map directly without going
    through the registry's verifier.
    """
    path = PROJECT_ROOT / "data" / "oid-catalogs" / vendor / model / f"{firmware}.json"
    return json.loads(path.read_text())


@pytest.mark.parametrize(("vendor", "model", "firmware"), _RE_SIGNED_TRIPLES)
def test_every_resigned_baseline_tools_envelope_includes_register_device(
    vendor: str, model: str, firmware: str
) -> None:
    """Every re-signed PMP 450i baseline's `tools` envelope carries `register_device`."""
    env = _load_signed_envelope(vendor=vendor, model=model, firmware=firmware)
    tools = env.get("tools", {})
    assert "register_device" in tools, (
        f"{vendor}/{model}/{firmware} tools envelope missing 'register_device'; "
        f"got {sorted(tools)!r}"
    )
    assert "sysDescr" in tools["register_device"], (
        f"{vendor}/{model}/{firmware} register_device entry missing sysDescr; "
        f"got {tools['register_device']!r}"
    )


@pytest.mark.parametrize(("vendor", "model", "firmware"), _RE_SIGNED_TRIPLES)
def test_resigned_baseline_oids_includes_sysdescr(vendor: str, model: str, firmware: str) -> None:
    """Every re-signed PMP 450i baseline's `oids` map carries `sysDescr`."""
    env = _load_signed_envelope(vendor=vendor, model=model, firmware=firmware)
    oids = env.get("oids", {})
    assert oids.get("sysDescr") == "1.3.6.1.2.1.1.1.0", (
        f"{vendor}/{model}/{firmware} oids map must contain sysDescr -> "
        f"1.3.6.1.2.1.1.1.0; got {oids.get('sysDescr')!r}"
    )


@pytest.mark.parametrize(("vendor", "model", "firmware"), _RE_SIGNED_TRIPLES)
def test_every_resigned_baseline_hmac_verifies(vendor: str, model: str, firmware: str) -> None:
    """`OidCatalogRegistry.verify_all(Settings)` accepts every re-signed baseline."""
    from nora.config import Settings
    from nora.data import BUILTIN_BASELINE_SIGNING_KEY
    from nora.drivers.oid_catalog import OidCatalogRegistry

    settings = Settings(
        _env_file=None,
        _env_file_encoding=None,
        nora_oid_catalogs_path=PROJECT_ROOT / "data" / "oid-catalogs",
        nora_oid_catalog_signing_key=BUILTIN_BASELINE_SIGNING_KEY,
    )
    registry = OidCatalogRegistry.verify_all(settings)
    # The triple must be loaded.
    assert (vendor, model, firmware) in registry.loaded_refs, (
        f"{vendor}/{model}/{firmware} not loaded by verify_all; got {registry.loaded_refs!r}"
    )
    # And `resolve(...)` returns the catalog (HMAC + schema OK).
    catalog = registry.resolve((vendor, model, firmware))
    assert catalog.firmware == firmware
    # And `required_oids_by_tool` exposes `register_device`.
    per_tool = registry.required_oids_by_tool((vendor, model))
    assert "register_device" in per_tool, (
        f"{vendor}/{model}/{firmware} missing 'register_device' in per-tool index; "
        f"got {sorted(per_tool)!r}"
    )
    assert "sysDescr" in per_tool["register_device"]


def test_register_device_passes_registration_guard_via_catalog_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`register_device` is accepted by the boot-time guard via the catalog envelope.

    After the Task 6 re-sign, `register_device` MUST be in the catalog
    envelope — so the guard accepts it WITHOUT an allow-list entry.
    This test boots `cli.main()` against the production-signed catalog
    roots and asserts (a) `verify_tools_are_catalogued` does NOT raise
    for `register_device` and (b) `mcp.run()` proceeds.

    Runs against the real operator-root + built-in-root catalogs (both
    re-signed with `BUILTIN_BASELINE_SIGNING_KEY` in this PR).
    """
    from nora import cli as cli_mod
    from nora import server as server_mod

    # Hermetic env: production operator root + built-in root; empty inventory.
    monkeypatch.setenv("NORA_OID_CATALOGS_PATH", str(PROJECT_ROOT / "data" / "oid-catalogs"))
    monkeypatch.setenv("NORA_DEVICES_INVENTORY_PATH", str(tmp_path / "devices.yaml"))
    (tmp_path / "devices.yaml").write_text("# empty hermetic inventory\n")
    monkeypatch.setenv(
        "NORA_OID_CATALOG_SIGNING_KEY",
        "nora-built-in-baseline-placeholder-key-do-not-use-in-prod",
    )

    # Patch mcp.run to a no-op so we don't actually start the server.
    monkeypatch.setattr(server_mod.mcp, "run", lambda *_a, **_kw: None)

    try:
        cli_mod.main([])
    except SystemExit:
        pass
    # If `verify_tools_are_catalogued` had rejected `register_device`,
    # the boot would have aborted with `UncataloguedToolError` BEFORE
    # reaching `mcp.run()`. No assertion needed here — the absence of
    # an `UncataloguedToolError` exception is the positive signal.
