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
