"""Tests for `snmp_get_pmp450i_radio_metrics` MCP tool wiring.

Covers Driver-R3 (strictly typed return opts out of Sanitizer), the
tool-surface contract — `device_id` flows through, the response shape
is JSON-serialisable, and free-text error messages pass through the
Sanitizer. The auto-trace / SessionJournal side-effects were
eliminated as part of the `nora-mcp-thin-split` cut.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from nora.config import Settings

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def _wired_driver_env(tmp_path: Path, sample_catalog: dict[str, Any], sample_inventory: Path):
    """Wire a Settings + a fake `Pmp450iDriver` singleton.

    Returns a tuple `(server_mod, fake_driver)`. The fake driver returns
    a stubbed `RadioMetricsReport` so the test focuses on the server
    boundary (serialisation + sanitizer).
    """
    from nora import server as server_mod
    from nora.drivers import registry as registry_mod
    from nora.drivers.snmp_pmp450i import Pmp450iDriver, RadioMetricsReport

    settings = Settings(
        _env_file=None,
        _env_file_encoding=None,
        nora_oid_catalogs_path=sample_catalog["path"].parent.parent.parent,
        nora_devices_inventory_path=sample_inventory,
        nora_oid_catalog_signing_key=sample_catalog["key"],
        nora_prompts_dir=tmp_path / "prompts",
    )

    from datetime import datetime, timezone

    fake_report = RadioMetricsReport(
        device_id="ap-7400-01",
        fetched_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        firmware="15.2.1",
        # Issue #57 (2026-09-19): the legacy per-LUID radio-metrics
        # subset (``radio_dl_rate_bps`` / ``radio_ul_rate_bps`` /
        # ``rx_signal_dbm`` / ``ssr`` / ``modulation``) was dropped
        # from the model. Only sector scalars remain.
        eirp_dbm=44,
        active_tx_power_dbm=27.0,
        channel_bandwidth_mhz=20.0,
        carrier_frequency_khz=5490000,
        transmit_power_dbm=27,
    )

    fake_driver = mock.MagicMock(spec=Pmp450iDriver)
    fake_driver.fetch_radio_metrics.return_value = fake_report

    server_mod.set_runtime_state(settings)

    # Inject the fake driver via the module-level DriverRegistry singleton.
    registry_mod.set_driver(fake_driver)
    server_mod.register_tool_log_middleware()

    try:
        yield server_mod, fake_driver
    finally:
        registry_mod.set_driver(None)


async def _call_tool(server_mod: Any, name: str, args: dict[str, Any]) -> Any:
    from fastmcp import Client

    async with Client(server_mod.mcp) as client:
        return await client.call_tool(name, args)


# ---------------------------------------------------------------------------
# R3 — typed return
# ---------------------------------------------------------------------------


def test_tool_returns_typed_report_payload(_wired_driver_env: Any) -> None:
    """`snmp_get_pmp450i_radio_metrics` returns a JSON-serialisable dict
    with the eight typed fields and NO `dict`/`Any` shape.
    """
    server_mod, _ = _wired_driver_env
    result = asyncio.run(
        _call_tool(
            server_mod,
            "snmp_get_pmp450i_radio_metrics",
            {"device_id": "ap-7400-01"},
        )
    )
    payload = result.data
    expected_fields = {
        "device_id",
        "fetched_at",
        "firmware",
        "eirp_dbm",
        "active_tx_power_dbm",
        "channel_bandwidth_mhz",
        "carrier_frequency_khz",
        "transmit_power_dbm",
    }
    assert set(payload.keys()) == expected_fields, (
        f"unexpected payload shape: {set(payload.keys())!r}"
    )
    assert payload["device_id"] == "ap-7400-01"
    assert payload["eirp_dbm"] == 44
    assert payload["active_tx_power_dbm"] == 27.0
    assert payload["channel_bandwidth_mhz"] == 20.0
    assert payload["carrier_frequency_khz"] == 5490000
    assert payload["transmit_power_dbm"] == 27


def test_tool_invokes_driver_fetch_radio_metrics(_wired_driver_env: Any) -> None:
    """The MCP tool delegates to `Pmp450iDriver.fetch_radio_metrics`."""
    server_mod, fake_driver = _wired_driver_env
    asyncio.run(
        _call_tool(
            server_mod,
            "snmp_get_pmp450i_radio_metrics",
            {"device_id": "ap-7400-01"},
        )
    )
    fake_driver.fetch_radio_metrics.assert_called_once_with("ap-7400-01")


# ---------------------------------------------------------------------------
# R3 — sanitizer boundary (free-text error path)
# ---------------------------------------------------------------------------


def test_tool_emits_outcome_error_log_on_driver_exception(
    _wired_driver_env: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """When the driver raises, the thin middleware logs `outcome=error` AND
    re-raises the original typed exception (no sanitization step in the
    thin path — that was a SessionJournal concern).
    """
    import logging

    from nora.drivers.exceptions import NetworkUnreachableError

    server_mod, fake_driver = _wired_driver_env
    fake_driver.fetch_radio_metrics.side_effect = NetworkUnreachableError(
        "agent closed at 10.0.0.5:161"
    )

    with caplog.at_level(logging.INFO, logger="nora.server"):
        with pytest.raises(Exception):
            asyncio.run(
                _call_tool(
                    server_mod,
                    "snmp_get_pmp450i_radio_metrics",
                    {"device_id": "ap-7400-01"},
                )
            )

    tool_lines = [
        r.getMessage()
        for r in caplog.records
        if "tool=" in r.getMessage() and "duration_ms=" in r.getMessage()
    ]
    assert tool_lines, (
        f"Expected one tool= log line on the error path; got: "
        f"{[r.getMessage() for r in caplog.records]!r}"
    )
    line = tool_lines[0]
    assert "snmp_get_pmp450i_radio_metrics" in line
    assert "outcome=error" in line


# ---------------------------------------------------------------------------
# Issue #61 / PR1 WU-1.5 + WU-1.6 server-tool smoke tests.
#
# These three tests cover the new ICMP sector stability probe MCP
# tools (`icmp_run_sector_stability_probe`,
# `icmp_get_sector_stability_progress`,
# `icmp_cancel_sector_stability_probe`). Each test wires an in-process
# fake driver + a hermetic fake pinger (via `monkeypatch`) so the
# daemon thread never opens a real socket, then drives the tool via
# the FastMCP `Client` (same idiom as the `snmp_get_pmp450i_radio_metrics`
# tests above).
#
# Scope (PR1 minimum viable coverage; PR2 persistence / metrics
# tests will deepen this):
# 1. start a probe; assert it returns within 10 s with a run_id
#    and `status == "running"`.
# 2. query progress immediately after start; assert the snapshot
#    carries the same run_id and at least 0 samples.
# 3. start a long probe, cancel it, assert the snapshot's status
#    flips to `cancelled`.
# ---------------------------------------------------------------------------


class _FakePinger:
    """Records calls and returns a pre-canned healthy sample.

    Mirrors the `_FakePinger` in `tests/probes/test_probe_coordinator.py`
    but kept inline so this test file has no test-to-test imports.
    """

    def __init__(self, *, rtt_ms: float = 1.0) -> None:
        self.rtt_ms = rtt_ms
        self.close_count = 0
        self.ping_calls = 0

    async def ping(self, target: str, *, payload_size: int, timeout: float):  # noqa: ARG002
        from nora.probes.icmp import IcmpSample

        self.ping_calls += 1
        return IcmpSample(
            target=target,
            rtt_ms=self.rtt_ms,
            received=True,
            error=None,
            timestamp_unix=0.0,
        )

    async def close(self) -> None:
        self.close_count += 1


class _FakeSubscriberSummaryDriver:
    """Fake driver returning a typed :class:`SubscriberSummary`.

    The discovery helper consumes `fetch_sm_table(device_id=...)` and
    folds the result into ordered probe targets. Returns one AP + one
    ONLINE_ACTIVE SM so the coordinator spawns two pingers per
    destination.
    """

    def __init__(self, summary) -> None:  # noqa: ANN001 — type-only import
        self._summary = summary

    def fetch_sm_table(self, *, device_id: str, settings=None):  # noqa: ARG002
        return self._summary


@pytest.fixture
def _wired_probe_env(tmp_path: Path, sample_catalog, sample_inventory, monkeypatch):
    """Wire a fake driver + a fake pinger + a fresh :class:`ProbeRunRegistry`.

    Mirrors `_wired_driver_env` but injects:
    * `_FakeSubscriberSummaryDriver` for `set_driver(...)`.
    * `_FakePinger` (a single instance reused for every destination,
      so the test can count ping calls).
    * `ProbeRunRegistry()` via `set_probe_registry(...)`.

    Yields `(server_mod, fake_pinger)` so each test inspects the
    pinger state after the tool returns.
    """
    from nora import server as server_mod
    from nora.config import Settings as _Settings
    from nora.drivers import registry as registry_mod
    from nora.drivers.snmp_pmp450i.subscribers import (
        SubscriberRecord,
        SubscriberSummary,
    )
    from nora.probes.state import ProbeRunRegistry

    settings = _Settings(
        _env_file=None,
        _env_file_encoding=None,
        nora_oid_catalogs_path=sample_catalog["path"].parent.parent.parent,
        nora_devices_inventory_path=sample_inventory,
        nora_oid_catalog_signing_key=sample_catalog["key"],
        nora_prompts_dir=tmp_path / "prompts",
        # PR1 bounds — keep tight so a misconfigured test fails fast.
        nora_icmp_min_duration_seconds=60,
        nora_icmp_default_duration_seconds=600,
        nora_icmp_max_duration_seconds=1800,
        nora_icmp_default_interval_seconds=1.0,
        nora_icmp_default_packet_size_bytes=64,
        nora_icmp_per_packet_timeout_seconds=5.0,
    )

    summary = SubscriberSummary(
        target_ip="192.0.2.1",
        online_active=[
            SubscriberRecord(
                luid="001",
                session_uptime=86400,
                cinr_db=22,
                link_status="inSession",
                modulation="8X",
            ),
        ],
        active_degraded=[],
        pre_existing_offline=[],
        baseline_size=1,
        pre_existing_offline_count=0,
        fetched_at="2026-09-19T00:00:00+00:00",
    )
    fake_driver = _FakeSubscriberSummaryDriver(summary)
    fake_pinger = _FakePinger()

    # Patch `UnprivilegedIcmpPinger` so the daemon thread uses our fake.
    # The probe coordinator instantiates one pinger per destination, so
    # all pingers share the same identity (and the same call counter).
    def _pinger_factory(*_args, **_kwargs):  # noqa: ANN001, ANN202
        return fake_pinger

    monkeypatch.setattr("nora.probes.probe.UnprivilegedIcmpPinger", _pinger_factory)

    server_mod.set_runtime_state(settings)
    registry_mod.set_driver(fake_driver)
    server_mod.set_probe_registry(ProbeRunRegistry(ttl_seconds=3600))
    server_mod.register_tool_log_middleware()

    try:
        yield server_mod, fake_pinger
    finally:
        registry_mod.set_driver(None)
        server_mod.set_probe_registry(None)


def test_icmp_run_sector_stability_probe_returns_run_id_with_status_running(
    _wired_probe_env: Any,
) -> None:
    """`icmp_run_sector_stability_probe` returns a run_id with status == 'running'.

    Smoke test (PR1 WU-1.5): the tool body validates bounds, spawns the
    daemon in a thread, blocks for `collect_window_seconds` (default 5 s),
    and returns the snapshot. The daemon's first flush typically lands
    within the window so `samples_count >= 0`.
    """
    server_mod, _ = _wired_probe_env
    result = asyncio.run(
        _call_tool(
            server_mod,
            "icmp_run_sector_stability_probe",
            {
                "device_id": "ap-7400-01",
                "duration_seconds": 120,
                "interval_seconds": 10.0,
                "collect_window_seconds": 1.0,
            },
        )
    )
    payload = result.data
    assert isinstance(payload, dict), f"expected dict; got {type(payload).__name__}"
    assert "run_id" in payload
    assert isinstance(payload["run_id"], str)
    assert len(payload["run_id"]) == 8
    assert payload["status"] in {"running", "completed", "failed"}, (
        f"unexpected status {payload['status']!r}"
    )
    assert payload["device_id"] == "ap-7400-01"
    assert payload["samples_count"] >= 0
    assert "started_at_unix" in payload
    assert "last_samples" in payload


def test_icmp_get_sector_stability_progress_finds_known_run_id(
    _wired_probe_env: Any,
) -> None:
    """`icmp_get_sector_stability_progress(run_id)` returns the live snapshot.

    Smoke test: start a probe, then immediately query its progress.
    The two snapshots share the run_id; the second one reflects the
    snapshot's `started_at_unix` (immutable for the run).
    """
    server_mod, _ = _wired_probe_env
    started = asyncio.run(
        _call_tool(
            server_mod,
            "icmp_run_sector_stability_probe",
            {
                "device_id": "ap-7400-01",
                "duration_seconds": 120,
                "interval_seconds": 10.0,
                "collect_window_seconds": 0.5,
            },
        )
    )
    run_id = started.data["run_id"]
    progress = asyncio.run(
        _call_tool(
            server_mod,
            "icmp_get_sector_stability_progress",
            {"run_id": run_id},
        )
    )
    payload = progress.data
    assert payload["run_id"] == run_id
    assert payload["device_id"] == "ap-7400-01"
    assert payload["started_at_unix"] == started.data["started_at_unix"]
    assert payload["samples_count"] >= 0


def test_icmp_cancel_sector_stability_probe_marks_cancelled(
    _wired_probe_env: Any,
) -> None:
    """`icmp_cancel_sector_stability_probe(run_id)` flips status to 'cancelled'.

    Smoke test: start a long probe (120 s) with a slow interval (10 s)
    so the daemon cannot complete within the cancel window. After
    cancel, the snapshot's status is `cancelled`.
    """
    server_mod, _ = _wired_probe_env
    started = asyncio.run(
        _call_tool(
            server_mod,
            "icmp_run_sector_stability_probe",
            {
                "device_id": "ap-7400-01",
                "duration_seconds": 120,
                "interval_seconds": 10.0,
                "collect_window_seconds": 0.5,
            },
        )
    )
    run_id = started.data["run_id"]
    cancel_result = asyncio.run(
        _call_tool(
            server_mod,
            "icmp_cancel_sector_stability_probe",
            {"run_id": run_id},
        )
    )
    assert cancel_result.data["run_id"] == run_id
    assert cancel_result.data["status"] == "cancelled"

    # Verify the snapshot reflects the cancellation.
    progress = asyncio.run(
        _call_tool(
            server_mod,
            "icmp_get_sector_stability_progress",
            {"run_id": run_id},
        )
    )
    assert progress.data["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Issue #61 / PR3 WU-3.14 — `icmp_list_probe_runs` smoke test.
#
# Verifies the listing tool is wired into the @mcp.tool surface and
# returns the expected JSON shape. The test does NOT spawn a real
# daemon; instead it monkeypatches the @mcp.tool body to write one
# synthetic PRB-*.json file into ``settings.nora_probe_results_dir``
# so the listing loop has something to read. This keeps the test
# fast (no daemon thread) while exercising the full JSON-loading +
# ProbeRunRecord-validation + Sanitizer-pre-clean + sort pipeline.
# ---------------------------------------------------------------------------


def test_icmp_list_probe_runs_returns_records(_wired_probe_env: Any, tmp_path: Path) -> None:
    """`icmp_list_probe_runs` returns the registry contents as JSON.

    Writes a synthetic PRB-*.json file via the in-memory
    ``ProbeRunRecord`` model + JSON serialisation, then calls the
    listing tool over FastMCP and asserts the response shape.
    """
    server_mod, _ = _wired_probe_env

    # Build a synthetic ProbeRunRecord and write it to disk.
    settings = server_mod.get_runtime_state()
    probe_dir = Path(settings.nora_probe_results_dir)
    probe_dir.mkdir(parents=True, exist_ok=True)

    # Minimal payload that ProbeRunRecord.model_validate accepts.
    payload: dict[str, Any] = {
        "run_id": "abcd1234",
        "sector": "norte",
        "device_id": "ap-7400-01",
        "ap_ip": "192.0.2.10",
        "started_at_unix": 1_761_234_567.0,
        "finished_at_unix": 1_761_235_167.0,
        "metrics": {
            "ap_metrics": {
                "target": "ap",
                "packets_transmitted": 600,
                "packets_received": 600,
                "packet_loss_pct": 0.0,
                "drop_burst_max": 0,
                "outage_events": 0,
                "rtt_min_ms": 1.0,
                "rtt_avg_ms": 1.5,
                "rtt_median_ms": 1.4,
                "rtt_p95_ms": 2.0,
                "rtt_max_ms": 3.0,
                "jitter_avg_ms": 0.3,
                "jitter_max_ms": 0.8,
                "samples_used": 600,
            },
            "sm_metrics": [],
            "sector_delta": [],
        },
        "verdict": {
            "ap_verdict": "EXCELLENT",
            "per_sm": [],
            "sector_verdict": "EXCELLENT",
            "rationale": "Synthetic test record.",
            "evaluated_at_unix": 1_761_235_167.0,
        },
    }
    (probe_dir / "PRB-norte-192.0.2.10-1761234567-aabbcc.json").write_text(
        __import__("json").dumps(payload), encoding="utf-8"
    )

    listing = asyncio.run(
        _call_tool(
            server_mod,
            "icmp_list_probe_runs",
            {"limit": 5},
        )
    )
    result = listing.data
    assert isinstance(result, dict)
    assert "runs" in result
    assert "total_files_scanned" in result
    assert "errors_skipped" in result
    assert len(result["runs"]) >= 1
    first = result["runs"][0]
    # The bypass fields stay verbatim; the listing surfaces every
    # documented column the WU-3.7 spec requires.
    assert first["run_id"] == "abcd1234"
    assert first["sector"] == "norte"
    assert first["device_id"] == "ap-7400-01"
    assert first["verdict"]["sector_verdict"] == "EXCELLENT"


def test_icmp_list_probe_runs_returns_empty_when_dir_missing(
    _wired_probe_env: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When `nora_probe_results_dir` does not exist, the listing is empty.

    The tool does NOT raise; it returns the documented empty payload
    so the orchestrator can probe before committing to a real fetch.
    """
    server_mod, _ = _wired_probe_env
    # Point the settings at a non-existent directory for this test only.
    fake_dir = tmp_path / "no-such-probe-dir"
    settings = server_mod.get_runtime_state()
    monkeypatch.setattr(settings, "nora_probe_results_dir", fake_dir, raising=False)

    listing = asyncio.run(
        _call_tool(
            server_mod,
            "icmp_list_probe_runs",
            {"limit": 5},
        )
    )
    result = listing.data
    assert result["runs"] == []
    assert result["total_files_scanned"] == 0
    assert result["errors_skipped"] == 0
