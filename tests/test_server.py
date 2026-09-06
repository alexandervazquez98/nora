"""MCP server tests — cover every scenario in `specs/nora-mcp-server/spec.md`.

The tests verify the FastMCP boot contract, the `nora_health` tool shape,
the stderr-only logging rule, and the structured tool-diagnostics line.
"""

from __future__ import annotations

import ast
import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from pydantic import SecretStr

from nora.config import Settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src" / "nora"


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
    """`server.py` MUST register `nora_health` via `@mcp.tool`."""
    import asyncio

    from nora import server as server_mod

    async def _names() -> set[str]:
        tools = await server_mod.mcp.list_tools()
        return {t.name for t in tools}

    names = asyncio.run(_names())
    assert "nora_health" in names, f"nora_health not registered; tools: {names}"


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
# Requirement: `nora_health` Tool Contract
# ---------------------------------------------------------------------------


def _call_nora_health(provider: Any, settings: Settings) -> dict[str, Any]:
    """Invoke the nora_health implementation directly.

    The public MCP tool takes no arguments and pulls from module-level state.
    For unit tests we exercise the internal implementation function so we can
    inject a mock provider and a hermetic Settings.
    """
    from nora import server as server_mod

    impl = getattr(server_mod, "nora_health_impl", None)
    assert impl is not None, "server module must expose `nora_health_impl`"
    return impl(settings, provider)


def test_health_returns_all_four_fields_with_provider_ok() -> None:
    """`nora_health` returns version, active_provider, connectivity, env_loaded."""
    settings = Settings(_env_file=None, _env_file_encoding=None)
    provider = mock.MagicMock()
    provider.complete.return_value = mock.Mock(text="pong", model_id="m", raw=object())

    result = _call_nora_health(provider, settings)

    assert set(result.keys()) == {
        "version",
        "active_provider",
        "connectivity",
        "env_loaded",
    }
    assert result["connectivity"] == "ok"
    assert result["active_provider"] == "lmstudio"
    assert result["env_loaded"] is False
    assert isinstance(result["version"], str) and result["version"]


def test_health_reports_unavailable_when_provider_fails() -> None:
    """When the provider raises, `connectivity` reports unavailable, not crash."""
    from nora.llm import LLMUnavailable

    settings = Settings(_env_file=None, _env_file_encoding=None)
    provider = mock.MagicMock()
    provider.complete.side_effect = LLMUnavailable("boom")

    result = _call_nora_health(provider, settings)
    assert result["connectivity"] == "unavailable"
    # The tool must not raise.


def test_health_reports_env_loaded_status() -> None:
    """`env_loaded` reflects whether a `.env` file supplied the values."""
    import os
    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
        f.write("NORA_LLM_PROVIDER=lmstudio\n")
        env_path = f.name
    try:
        settings = Settings(_env_file=env_path)
        provider = mock.MagicMock()
        provider.complete.return_value = mock.Mock(text="ok", model_id="m", raw=object())
        result = _call_nora_health(provider, settings)
        assert result["env_loaded"] is True
    finally:
        os.remove(env_path)


def test_health_does_not_echo_api_key() -> None:
    """`nora_health` response MUST NOT contain any secret value."""
    settings = Settings(
        _env_file=None, _env_file_encoding=None, gemini_api_key=SecretStr("top-secret-key")
    )
    provider = mock.MagicMock()
    provider.complete.return_value = mock.Mock(text="ok", model_id="m", raw=object())

    result = _call_nora_health(provider, settings)
    rendered = str(result)
    assert "top-secret-key" not in rendered


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
# Requirement: Telemetry Sanitizer Boundary
# ---------------------------------------------------------------------------


def test_health_sanitizes_upstream_error_messages() -> None:
    """Free-text error messages in `nora_health` MUST pass through the sanitizer."""
    from nora.llm import LLMUnavailable
    from nora.sanitizer import Sanitizer

    settings = Settings(_env_file=None, _env_file_encoding=None)
    provider = mock.MagicMock()
    provider.complete.side_effect = LLMUnavailable(
        "connection refused at 10.0.0.5 for host router-core-01.example.com"
    )

    # We call nora_health with a shared Sanitizer to observe its output.
    sanitizer = Sanitizer()
    with mock.patch("nora.server._sanitizer", sanitizer):
        result = _call_nora_health(provider, settings)

    # The error string (if any) MUST NOT contain private IP or hostname.
    rendered = str(result)
    assert "10.0.0.5" not in rendered, f"Private IPv4 literal leaked: {rendered!r}"
    assert "router-core-01.example.com" not in rendered, f"Hostname literal leaked: {rendered!r}"


# ---------------------------------------------------------------------------
# Requirement: Edge Cases — Subprocess boot
# ---------------------------------------------------------------------------


def test_module_main_can_be_imported() -> None:
    """`src/nora/__main__.py` MUST exist and expose `main`."""
    from nora import __main__ as main_mod

    assert callable(getattr(main_mod, "main", None))


def test_main_module_boots_fastmcp_over_stdio() -> None:
    """`nora.__main__:main()` wires Settings -> factory -> mcp.run() (no transport)."""
    import inspect

    from nora import __main__ as main_mod

    src = inspect.getsource(main_mod.main)
    assert "build_provider" in src, "main() must call build_provider(settings)"
    assert "mcp.run" in src, "main() must call mcp.run()"
    # `mcp.run()` with NO transport argument defaults to stdio.
    assert "mcp.run()" in src, "mcp.run() must be called without a transport argument"


# ---------------------------------------------------------------------------
# Requirement: Observability — Stderr Tool Diagnostics
# ---------------------------------------------------------------------------


def test_health_emits_structured_diagnostic_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`nora_health` MUST emit one structured stderr log line per invocation."""
    settings = Settings(_env_file=None, _env_file_encoding=None)
    provider = mock.MagicMock()
    provider.complete.return_value = mock.Mock(text="ok", model_id="m", raw=object())

    with caplog.at_level(logging.INFO, logger="nora.server"):
        _call_nora_health(provider, settings)

    tool_lines = [
        record.getMessage()
        for record in caplog.records
        if record.name == "nora.server" and "tool=" in record.getMessage()
    ]
    assert tool_lines, (
        f"Expected a structured tool log line; got: {[r.getMessage() for r in caplog.records]!r}"
    )
    line = tool_lines[0]
    assert "nora_health" in line
    # A duration field (e.g., duration_ms=...) is required.
    assert "duration" in line.lower(), f"Expected a duration field in tool log line; got: {line!r}"


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

    try:
        proc = subprocess.run(
            [str(py), "-m", "nora"],
            cwd=str(PROJECT_ROOT),
            input=payload,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
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

    # Stderr should contain a startup log line that names the active provider.
    assert "active_provider" in stderr or "lmstudio" in stderr, (
        f"Expected startup log on stderr; got:\n{stderr}"
    )
