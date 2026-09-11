"""Tests for `snmp_get_pmp450i_radio_metrics` MCP tool wiring.

Covers Driver-R3 (strictly typed return opts out of Sanitizer), R4
(auto-trace middleware records the tool call), and the tool-surface
contract — `device_id` flows through, the response shape is JSON-serialisable,
and free-text error messages pass through the Sanitizer.
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
    """Wire a Settings + journal + a fake `Pmp450iDriver` singleton.

    Returns a tuple `(server_mod, fake_driver)`. The fake driver returns
    a stubbed `RadioMetricsReport` so the test focuses on the server
    boundary (serialisation + sanitizer + auto-trace).
    """
    from nora import server as server_mod
    from nora.drivers import registry as registry_mod
    from nora.drivers.snmp_pmp450i import Pmp450iDriver, RadioMetricsReport

    settings = Settings(
        _env_file=None,
        _env_file_encoding=None,
        nora_session_journal_dir=tmp_path / "sessions",
        nora_session_trace_max_steps=10,
        nora_session_journal_enabled=True,
        nora_operator_alias="wiring-op",
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
        radio_dl_rate_bps=54000000,
        radio_ul_rate_bps=21000000,
        rx_signal_dbm=-58,
        tx_signal_dbm=23,
        ssr=75,
        modulation="256QAM",
    )

    fake_driver = mock.MagicMock(spec=Pmp450iDriver)

    def _fake_fetch(device_id: str) -> Any:
        # Mirror the real driver's behaviour: set focus before fetch.
        from nora.drivers.snmp_pmp450i.driver import nora_session_set_focus

        nora_session_set_focus(device_id)
        return fake_report

    fake_driver.fetch_radio_metrics.side_effect = _fake_fetch

    provider = mock.MagicMock()
    provider.complete.return_value = mock.Mock(text="ok", model_id="m", raw=object())
    server_mod.set_runtime_state(settings, provider)
    server_mod.init_session_journal(settings)

    # Inject the fake driver via the module-level DriverRegistry singleton.
    registry_mod.set_driver(fake_driver)
    server_mod.register_auto_trace_middleware()

    try:
        yield server_mod, fake_driver
    finally:
        server_mod.unregister_auto_trace_middleware()
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
        "radio_dl_rate_bps",
        "radio_ul_rate_bps",
        "rx_signal_dbm",
        "tx_signal_dbm",
        "ssr",
        "modulation",
    }
    assert set(payload.keys()) == expected_fields, (
        f"unexpected payload shape: {set(payload.keys())!r}"
    )
    assert payload["device_id"] == "ap-7400-01"
    assert payload["radio_dl_rate_bps"] == 54000000
    assert payload["modulation"] == "256QAM"


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


def test_tool_error_message_passes_through_sanitizer(_wired_driver_env: Any) -> None:
    """When the driver raises with an IPv4 literal, the surfaced
    exception message MUST be sanitized.

    The auto-trace middleware routes the error through
    `journal._sanitizer`; the private IPv4 literal in the message must
    be replaced with a synthetic alias before persistence. We assert
    the rule by inspecting the in-memory `state.trace[-1]` after the
    failing call.
    """
    from nora.core.session_journal import get_journal
    from nora.drivers.exceptions import NetworkUnreachableError

    server_mod, fake_driver = _wired_driver_env
    fake_driver.fetch_radio_metrics.side_effect = NetworkUnreachableError(
        "agent closed at 10.0.0.5:161"
    )

    with pytest.raises(Exception):
        asyncio.run(
            _call_tool(
                server_mod,
                "snmp_get_pmp450i_radio_metrics",
                {"device_id": "ap-7400-01"},
            )
        )

    state = get_journal().get_state()
    last = state.trace[-1]
    assert last.outcome == "error"
    assert "10.0.0.5" not in last.result_summary, (
        f"Private IPv4 leaked into the journal: {last.result_summary!r}"
    )


# ---------------------------------------------------------------------------
# R4 — auto-trace records a SessionStep
# ---------------------------------------------------------------------------


def test_tool_call_records_session_step(_wired_driver_env: Any) -> None:
    """Every successful tool call records exactly one auto-trace step."""
    from nora.core.session_journal import get_journal

    server_mod, _ = _wired_driver_env
    asyncio.run(
        _call_tool(
            server_mod,
            "snmp_get_pmp450i_radio_metrics",
            {"device_id": "ap-7400-01"},
        )
    )
    state = get_journal().get_state()
    steps = [s for s in state.trace if s.tool == "snmp_get_pmp450i_radio_metrics"]
    assert len(steps) == 1
    assert steps[0].outcome == "success"
    assert steps[0].input.get("device_id") == "ap-7400-01"


def test_tool_call_sets_focus_before_fetch(_wired_driver_env: Any) -> None:
    """The driver calls `set_focus` first; the journal reflects the focus
    binding before the fetch returns.
    """
    from nora.core.session_journal import get_journal

    server_mod, _ = _wired_driver_env
    asyncio.run(
        _call_tool(
            server_mod,
            "snmp_get_pmp450i_radio_metrics",
            {"device_id": "ap-7400-01"},
        )
    )
    state = get_journal().get_state()
    assert state.focus_device_id == "ap-7400-01"
    assert "ap-7400-01" in state.devices_reviewed
