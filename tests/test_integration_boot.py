"""Integration tests for the thin MCP entry points.

Boots `nora-mcp` and `python -m nora` as real subprocesses and asserts
both expose the same four-tool surface in the same order (per spec
R-NEW-1-Scenario "alias and `nora-mcp` expose identical tool lists").
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _venv_python() -> str:
    py = PROJECT_ROOT / ".venv" / "bin" / "python"
    if not py.exists():
        pytest.skip("venv python not present")
    return str(py)


def _read_until_id(
    lines: "queue.Queue[str]", target_id: int, *, timeout: float = 15.0
) -> str | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            line = lines.get(timeout=0.1)
        except queue.Empty:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("id") == target_id:
            return line
    return None


def _drive_tools_list(
    cmd: list[str],
    cwd: str,
    env: dict[str, str],
    *,
    tool_list_id: int = 2,
    timeout: float = 15.0,
) -> tuple[list[str], str]:
    """Run `cmd` as a subprocess, drive the MCP handshake, return (tools_in_order, stderr)."""
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
    initialized_notif = {
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
    }
    tools_list_frame = {
        "jsonrpc": "2.0",
        "id": tool_list_id,
        "method": "tools/list",
    }

    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        bufsize=1,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None
    assert proc.stderr is not None

    stdout_q: "queue.Queue[str]" = queue.Queue()
    stderr_chunks: list[str] = []

    def _drain_stdout() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            stdout_q.put(line)
        stdout_q.put("")

    def _drain_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_chunks.append(line)

    t_out = threading.Thread(target=_drain_stdout, daemon=True)
    t_err = threading.Thread(target=_drain_stderr, daemon=True)
    t_out.start()
    t_err.start()

    tool_names_in_order: list[str] = []
    try:
        proc.stdin.write(json.dumps(init_frame) + "\n")
        proc.stdin.flush()
        init_reply = _read_until_id(stdout_q, target_id=1, timeout=timeout)
        assert init_reply is not None, (
            f"Server never sent initialize reply (id=1).\nstderr so far: {''.join(stderr_chunks)!r}"
        )

        proc.stdin.write(json.dumps(initialized_notif) + "\n")
        proc.stdin.flush()
        proc.stdin.write(json.dumps(tools_list_frame) + "\n")
        proc.stdin.flush()

        tools_reply = _read_until_id(stdout_q, target_id=tool_list_id, timeout=timeout)
        assert tools_reply is not None, (
            f"Server never sent tools/list reply (id={tool_list_id}).\n"
            f"stderr so far: {''.join(stderr_chunks)!r}"
        )
        parsed = json.loads(tools_reply)
        tool_names_in_order = [t.get("name") for t in parsed["result"].get("tools", [])]

        proc.stdin.close()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        t_out.join(timeout=2)
        t_err.join(timeout=2)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        t_out.join(timeout=1)
        t_err.join(timeout=1)

    return tool_names_in_order, "".join(stderr_chunks)


def _boot_env(tmp_path: Path) -> dict[str, str]:
    """A hermetic env that boots the thin MCP without network deps."""
    (tmp_path / "catalogs").mkdir(exist_ok=True)
    (tmp_path / "devices.yaml").write_text("# empty\n")
    return {
        **os.environ,
        "NORA_OID_CATALOG_SIGNING_KEY": "test-boot-key",
        "NORA_OID_CATALOGS_PATH": str(tmp_path / "catalogs"),
        "NORA_DEVICES_INVENTORY_PATH": str(tmp_path / "devices.yaml"),
    }


def test_subprocess_nora_mcp_exposes_four_tools(tmp_path: Path) -> None:
    """Boot the `nora-mcp` console script and assert the 4-tool surface."""
    # The `nora-mcp` console script lives in `.venv/bin/`. uv installs it
    # from the `[project.scripts]` entry in `pyproject.toml`.
    nora_mcp = PROJECT_ROOT / ".venv" / "bin" / "nora-mcp"
    if not nora_mcp.exists():
        pytest.skip("nora-mcp console script not installed")

    tool_names, stderr = _drive_tools_list(
        [str(nora_mcp)],
        cwd=str(PROJECT_ROOT),
        env=_boot_env(tmp_path),
    )

    expected = [
        "snmp_get_pmp450i_radio_metrics",
        "search_intervention_history",
        "get_device_lifecycle_summary",
        "correlate_sector_interference",
    ]
    assert tool_names == expected, (
        f"`nora-mcp` must expose exactly the 4 thin tools in order; got {tool_names!r}"
    )
    # The boot log line is on stderr.
    assert "nora-mcp boot complete" in stderr, f"Expected startup log on stderr; got: {stderr!r}"


def test_subprocess_python_dash_m_nora_exposes_same_tools(tmp_path: Path) -> None:
    """Boot `python -m nora` and assert the SAME 4-tool surface as nora-mcp."""
    py = _venv_python()

    tool_names, stderr = _drive_tools_list(
        [py, "-m", "nora"],
        cwd=str(PROJECT_ROOT),
        env=_boot_env(tmp_path),
    )

    expected = [
        "snmp_get_pmp450i_radio_metrics",
        "search_intervention_history",
        "get_device_lifecycle_summary",
        "correlate_sector_interference",
    ]
    assert tool_names == expected, (
        f"`python -m nora` must expose exactly the 4 thin tools in order; got {tool_names!r}"
    )
    # DeprecationWarning is emitted on stderr.
    assert "DeprecationWarning" in stderr, (
        f"`python -m nora` must emit DeprecationWarning on stderr; got: {stderr!r}"
    )
    assert "will be removed in the next minor release" in stderr


def test_subprocess_both_entry_points_expose_identical_tool_lists(tmp_path: Path) -> None:
    """`nora-mcp` and `python -m nora` expose the four-tool surface in the SAME order."""
    py = _venv_python()
    nora_mcp = PROJECT_ROOT / ".venv" / "bin" / "nora-mcp"
    if not nora_mcp.exists():
        pytest.skip("nora-mcp console script not installed")

    canonical, _ = _drive_tools_list(
        [str(nora_mcp)],
        cwd=str(PROJECT_ROOT),
        env=_boot_env(tmp_path),
    )
    alias, _ = _drive_tools_list(
        [py, "-m", "nora"],
        cwd=str(PROJECT_ROOT),
        env=_boot_env(tmp_path),
    )

    assert canonical == alias, (
        f"`nora-mcp` and `python -m nora` must expose identical tool lists; "
        f"canonical: {canonical!r}, alias: {alias!r}"
    )
