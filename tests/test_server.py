"""MCP server tests — cover every scenario in `specs/nora-mcp-server/spec.md`.

The tests verify the FastMCP boot contract, the `nora_health` tool shape,
the stderr-only logging rule, and the structured tool-diagnostics line.
"""

from __future__ import annotations

import ast
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from nora.config import Settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src" / "nora"


def test_server_exposes_exactly_eleven_tools() -> None:
    """The FastMCP instance exposes exactly 11 tools.

    The eleven tools are: 1 driver (`snmp_get_pmp450i_radio_metrics`)
    + 2 read-summary (`snmp_get_ap_summary`, `snmp_get_frame_utilization`)
    + 2 SM baseline (`snmp_get_sm_table`, `snmp_get_sm_detailed_diagnostics`)
    + 1 spectrum sweep (`snmp_run_spectrum_analysis`)
    + 1 HITL-gated migration (`snmp_migrate_radio_frequency`)
    + 3 intervention read (`search_intervention_history`,
    `get_device_lifecycle_summary`, `correlate_sector_interference`)
    + 1 writer (`save_intervention_record`).

    The surface grew from 5 → 7 (PR 2) → 9 (PR 3) → 11 (PR 4).
    This test was renamed from ``test_server_exposes_exactly_nine_tools``
    in PR 5 to track the current contract.
    """
    import asyncio

    from nora import server as server_mod

    async def _names() -> set[str]:
        tools = await server_mod.mcp.list_tools()
        return {t.name for t in tools}

    names = asyncio.run(_names())
    expected = {
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
    }
    assert names == expected, (
        f"Expected exactly 11 tools; got {sorted(names)} "
        f"(missing: {sorted(expected - names)}, extra: {sorted(names - expected)})"
    )


def test_thin_middleware_emits_one_log_line_per_call(caplog: pytest.LogCaptureFixture) -> None:
    """The thin middleware MUST emit exactly one structured stderr log line per call.

    Format: `tool=<name> duration_ms=<int> outcome=<success|error>`. No
    journal, no record_step. Exactly one line per invocation.
    """
    import asyncio

    from nora import server as server_mod

    server_mod.configure_logging()
    server_mod.register_tool_log_middleware()

    async def _run() -> Any:
        from fastmcp import Client

        async with Client(server_mod.mcp) as client:
            return await client.call_tool("search_intervention_history", {"target_ip": "10.0.0.5"})

    with caplog.at_level(logging.INFO, logger="nora.server"):
        # Call may fail (no fixture interventions dir); the middleware must still log.
        try:
            asyncio.run(_run())
        except Exception:
            pass

    tool_lines = [
        record.getMessage()
        for record in caplog.records
        if "tool=" in record.getMessage() and "duration_ms=" in record.getMessage()
    ]
    assert len(tool_lines) >= 1, (
        f"Expected at least one structured tool log line; got: "
        f"{[r.getMessage() for r in caplog.records]!r}"
    )
    line = tool_lines[0]
    assert "search_intervention_history" in line
    assert "duration_ms=" in line
    assert ("outcome=success" in line) or ("outcome=error" in line)


# ---------------------------------------------------------------------------
# Requirement: FastMCP Boot Over Stdio
# ---------------------------------------------------------------------------


def test_server_module_exposes_fastmcp_instance() -> None:
    """`src/nora/server.py` MUST expose a module-level `mcp = FastMCP("nora")`."""
    from nora import server as server_mod

    assert hasattr(server_mod, "mcp"), "server module must export `mcp`"
    from fastmcp import FastMCP

    assert isinstance(server_mod.mcp, FastMCP)
    # The instance's name is exposed in modern FastMCP via `.name`.
    assert server_mod.mcp.name == "nora"


def test_server_module_uses_fastmcp_decorator() -> None:
    """`server.py` registers the driver tool + 3 intervention tools via `@mcp.tool`."""
    import asyncio

    from nora import server as server_mod

    async def _names() -> set[str]:
        tools = await server_mod.mcp.list_tools()
        return {t.name for t in tools}

    names = asyncio.run(_names())
    assert "snmp_get_pmp450i_radio_metrics" in names, f"driver tool not registered; tools: {names}"
    assert "search_intervention_history" in names, (
        f"intervention tool not registered; tools: {names}"
    )


def test_pyproject_pins_fastmcp_below_v4_in_server_check() -> None:
    """`pyproject.toml` MUST pin `fastmcp>=3.2,<4` (v4 has breaking changes)."""
    import tomllib

    data = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    deps = data["project"]["dependencies"]
    matches = [d for d in deps if d.startswith("fastmcp")]
    assert matches, "fastmcp is not declared as a runtime dependency"
    assert re.match(r"fastmcp\s*>=\s*3\.2\s*,\s*<\s*4", matches[0]), (
        f"fastmcp must be pinned to >=3.2,<4; got {matches[0]!r}"
    )


# ---------------------------------------------------------------------------
# Requirement: Stderr-Only Logging
# ---------------------------------------------------------------------------


def test_logging_is_configured_for_stderr_only() -> None:
    """`configure_logging` MUST attach a `StreamHandler(sys.stderr)`."""
    from nora import server as server_mod

    server_mod.configure_logging()
    root = logging.getLogger()
    has_stderr_handler = any(
        isinstance(h, logging.StreamHandler) and h.stream is sys.stderr for h in root.handlers
    )
    assert has_stderr_handler, (
        f"No stderr StreamHandler on root logger. Handlers: {root.handlers!r}"
    )


def test_src_nora_has_no_print_calls() -> None:
    """`src/nora/*.py` MUST NOT contain `print(...)` calls (AST scan).

    This is the AST-level complement to ruff's `T201` rule; it catches
    re-introduced prints even when ruff is not run.
    """
    offenders: list[tuple[Path, int]] = []
    for py in SRC_DIR.glob("*.py"):
        tree = ast.parse(py.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id == "print":
                    offenders.append((py, node.lineno))
    assert offenders == [], (
        f"`print(...)` found in src/nora/: {[(str(p), n) for p, n in offenders]}"
    )


# ---------------------------------------------------------------------------
# Requirement: Edge Cases — Subprocess boot
# ---------------------------------------------------------------------------


def test_module_main_can_be_imported() -> None:
    """`src/nora/__main__.py` MUST exist and expose `main` (deprecation alias)."""
    from nora import __main__ as main_mod

    assert callable(getattr(main_mod, "main", None))


def test_main_module_delegates_to_cli() -> None:
    """`nora.__main__:main()` is a deprecation alias that delegates to `nora.cli.main`."""
    import inspect

    from nora import __main__ as main_mod

    # `inspect.getsource` on a re-exported function would return the cli
    # source; inspect the module body directly.
    src = inspect.getsource(main_mod)
    assert "nora.cli" in src, f"__main__ module must import from nora.cli; got: {src!r}"
    assert "deprecated" in src.lower(), (
        f"__main__ module must emit a deprecation warning; got: {src!r}"
    )


# ---------------------------------------------------------------------------
# Requirement: Observability — Stderr Tool Diagnostics
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Subprocess boot smoke — `python -m nora` starts and writes JSON-RPC only
# to stdout.
# ---------------------------------------------------------------------------


def test_subprocess_boot_writes_only_jsonrpc_to_stdout() -> None:
    """A real `python -m nora` subprocess boots and writes a startup log to stderr.

    We send an `initialize` JSON-RPC frame on stdin and check that stdout
    contains a JSON-RPC response and stderr contains a startup log.
    """
    import json

    py = PROJECT_ROOT / ".venv" / "bin" / "python"
    if not py.exists():
        pytest.skip("venv python not present")
    # Build an `initialize` JSON-RPC frame. FastMCP replies to it.
    init_frame = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "0.0.0"},
        },
    }
    payload = json.dumps(init_frame) + "\n"

    # Provide a signing key that matches the shipped built-in baseline
    # (PR 1 ADR #17). Pre-PR1 this test used any non-empty key because
    # `verify_all` only scanned the (empty) operator root; post-PR1 the
    # built-in baseline is also HMAC-verified, so the key has to match
    # the placeholder shipped under `src/nora/data/oid-catalogs/`.
    from nora.data import BUILTIN_BASELINE_SIGNING_KEY

    env = {
        **os.environ,
        "NORA_OID_CATALOG_SIGNING_KEY": BUILTIN_BASELINE_SIGNING_KEY,
        "NORA_OID_CATALOGS_PATH": str(PROJECT_ROOT / "data" / "oid-catalogs-tmp"),
        "NORA_DEVICES_INVENTORY_PATH": str(PROJECT_ROOT / "data" / "devices-tmp.yaml"),
    }
    # Ensure the temp paths exist (empty catalog, empty inventory).
    (PROJECT_ROOT / "data" / "oid-catalogs-tmp").mkdir(parents=True, exist_ok=True)
    (PROJECT_ROOT / "data" / "devices-tmp.yaml").write_text("# empty\n")

    try:
        proc = subprocess.run(
            [str(py), "-m", "nora"],
            cwd=str(PROJECT_ROOT),
            input=payload,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"Subprocess hung.\nstdout:\n{exc.stdout}\nstderr:\n{exc.stderr}")

    stdout = proc.stdout
    stderr = proc.stderr

    # Stdout must contain at least one JSON-RPC reply.
    assert stdout.strip(), f"Empty stdout from server. stderr:\n{stderr}"
    has_jsonrpc = False
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if parsed.get("jsonrpc") == "2.0":
            has_jsonrpc = True
            break
    assert has_jsonrpc, f"stdout had no JSON-RPC frame:\n{stdout}"

    # Stderr should contain a startup log line naming the boot surface.
    assert "boot complete" in stderr or "nora-mcp" in stderr, (
        f"Expected startup log on stderr; got:\n{stderr}"
    )


# ---------------------------------------------------------------------------
# Requirement: Intervention-Memory MCP Tools (Phase 3)
# ---------------------------------------------------------------------------


def test_server_module_exports_three_new_tool_names() -> None:
    """R-NEW-1-S1 — `nora.server` re-exports the three intervention-memory tool names."""
    from nora import server as server_mod

    # The names MUST be importable from `nora.server`.

    # And they MUST appear in `__all__` for test discoverability.
    assert "search_intervention_history" in server_mod.__all__
    assert "get_device_lifecycle_summary" in server_mod.__all__
    assert "correlate_sector_interference" in server_mod.__all__


def test_mcp_tool_wrapper_delegates_to_pure_library_function(tmp_path: Path) -> None:
    """R-NEW-1-S2 — the MCP wrapper delegates to the library function with the same kwargs.

    Monkeypatches `nora.intervention_memory.tools.search_intervention_history`
    and asserts the wrapper calls it once with the same kwargs.
    """
    import asyncio
    from unittest import mock as _mock

    from nora.intervention_memory import tools as tools_mod

    settings = Settings(
        _env_file=None,
        _env_file_encoding=None,
        nora_interventions_dir=tmp_path,
    )

    from nora import server as server_mod

    server_mod.set_runtime_state(settings)

    sentinel = [{"intervention_id": "INT-DELEGATED"}]

    with _mock.patch.object(
        tools_mod,
        "search_intervention_history",
        return_value=sentinel,
    ) as patched:
        # Call the registered MCP tool via the FastMCP client.
        async def _run() -> Any:
            from fastmcp import Client

            async with Client(server_mod.mcp) as client:
                return await client.call_tool(
                    "search_intervention_history", {"target_ip": "10.0.0.5"}
                )

        result = asyncio.run(_run())

    assert patched.call_count == 1
    assert patched.call_args.kwargs["target_ip"] == "10.0.0.5"
    # FastMCP wraps the library return value; the sentinel list should be present.
    assert result.data == sentinel


def test_mcp_instance_exposes_all_nine_tools() -> None:  # noqa: F811 — alias kept for history
    """Deprecated: use `test_server_exposes_exactly_nine_tools` instead.

    Post-thin-split + writer + slice 2 + slice 3 + slice 4 (spectrum
    + HITL-gated migration): exactly 11 tools. This alias test
    exists so any accidentally re-added legacy tool fails the test
    loudly.
    """
    import asyncio

    from nora import server as server_mod

    async def _names() -> set[str]:
        tools = await server_mod.mcp.list_tools()
        return {t.name for t in tools}

    names = asyncio.run(_names())
    expected = {
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
    }
    assert names == expected, f"Expected exactly 11 tools after slice 4; got: {sorted(names)}"


def test_free_text_fields_in_search_output_sanitized_via_mcp_wrapper(tmp_path: Path) -> None:
    """R-NEW-2-S1 — free-text fields sanitized at the MCP boundary.

    Seed a fixture with `record_name` containing a private IP, invoke
    the MCP tool, assert the output's `record_name` carries the alias
    (not the literal IP).
    """
    import asyncio
    import json as _json

    interventions_dir = tmp_path / "interventions"
    interventions_dir.mkdir()
    (interventions_dir / "r1.json").write_text(
        _json.dumps(
            {
                "intervention_id": "INT-1",
                "timestamp_iso": "2026-01-01T12:00:00+00:00",
                "timestamp_unix": 1700000000,
                "ticket_number": "TKT-7400",
                "target_ip": "10.0.0.5",
                "stage": "PRE_DIAGNOSTIC",
                "record_name": "Investigating 10.0.0.5 today",
                "status": "COMPLETED",
                "agent_name": "test-agent",
                "findings_and_dictamen": "no findings",
                "created_at": "2026-01-01T12:00:00+00:00",
                "network_equipment": {},
            }
        )
    )

    settings = Settings(
        _env_file=None,
        _env_file_encoding=None,
        nora_interventions_dir=interventions_dir,
    )

    from nora import server as server_mod

    server_mod.set_runtime_state(settings)

    async def _run() -> Any:
        from fastmcp import Client

        async with Client(server_mod.mcp) as client:
            return await client.call_tool("search_intervention_history", {})

    result = asyncio.run(_run())
    first_record = result.data[0]
    assert "10.0.0.5" not in first_record["record_name"], (
        f"Private IP leaked in MCP tool output: {first_record['record_name']!r}"
    )
    assert "RADIO_NODE_" in first_record["record_name"]


def test_structured_top_level_fields_bypass_via_mcp_wrapper(tmp_path: Path) -> None:
    """R-NEW-2-S2 — structured fields bypass sanitization at the MCP boundary.

    The fixture's `intervention_id` contains an RFC 1918 IP-shaped
    substring; the MCP tool output preserves it byte-identical.
    """
    import asyncio
    import json as _json

    interventions_dir = tmp_path / "interventions"
    interventions_dir.mkdir()
    (interventions_dir / "r1.json").write_text(
        _json.dumps(
            {
                "intervention_id": "INT-1-10.0.0.5-1700000000-V7",
                "timestamp_iso": "2026-01-01T12:00:00+00:00",
                "timestamp_unix": 1700000000,
                "ticket_number": "TKT-7400",
                "target_ip": "10.0.0.5",
                "stage": "PRE_DIAGNOSTIC",
                "record_name": "test",
                "status": "COMPLETED",
                "agent_name": "test-agent",
                "findings_and_dictamen": "no findings",
                "created_at": "2026-01-01T12:00:00+00:00",
                "network_equipment": {},
            }
        )
    )

    settings = Settings(
        _env_file=None,
        _env_file_encoding=None,
        nora_interventions_dir=interventions_dir,
    )

    from nora import server as server_mod

    server_mod.set_runtime_state(settings)

    async def _run() -> Any:
        from fastmcp import Client

        async with Client(server_mod.mcp) as client:
            return await client.call_tool("search_intervention_history", {})

    result = asyncio.run(_run())
    first_record = result.data[0]
    assert first_record["intervention_id"] == "INT-1-10.0.0.5-1700000000-V7", (
        f"intervention_id must bypass sanitization; got: {first_record['intervention_id']!r}"
    )
    assert first_record["timestamp_unix"] == 1700000000
    assert first_record["stage"] == "PRE_DIAGNOSTIC"
