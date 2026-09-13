"""Subprocess test for `python -m nora` deprecation alias.

Boots the `python -m nora` entry point as a real subprocess, captures
stderr, and asserts:

1. stderr contains `DeprecationWarning` mentioning "will be removed in
   the next minor release".
2. The server still boots (the alias delegates to `nora.cli.main`).
3. The nine-tool surface is reachable via JSON-RPC `tools/list`.
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


def test_python_dash_m_nora_emits_deprecation_warning(tmp_path: Path) -> None:
    """`python -m nora` MUST emit a DeprecationWarning then delegate to nora-mcp.

    The stderr text must contain:
      * `DeprecationWarning` (Python's deprecation marker)
      * "will be removed in the next minor release"

    The test also confirms the server still boots successfully (the
    alias body invokes `cli.main()` which boots the nine-tool surface).
    """
    py = _venv_python()

    # PR 1 (ADR #17): the registry also HMAC-verifies the shipped
    # built-in baseline at `src/nora/data/oid-catalogs/`. The key here
    # MUST match the placeholder that baseline was signed with, otherwise
    # the server aborts at boot. See `nora.data.BUILTIN_BASELINE_SIGNING_KEY`.
    from nora.data import BUILTIN_BASELINE_SIGNING_KEY

    env = {
        **os.environ,
        "NORA_OID_CATALOG_SIGNING_KEY": BUILTIN_BASELINE_SIGNING_KEY,
        "NORA_OID_CATALOGS_PATH": str(tmp_path / "catalogs"),
        "NORA_DEVICES_INVENTORY_PATH": str(tmp_path / "devices.yaml"),
    }
    (tmp_path / "catalogs").mkdir(exist_ok=True)
    (tmp_path / "devices.yaml").write_text("# empty\n")

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
    tools_list_frame = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}

    proc = subprocess.Popen(
        [py, "-m", "nora"],
        cwd=str(PROJECT_ROOT),
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

    stdout_chunks: list[str] = []
    stdout_q: "queue.Queue[str]" = queue.Queue()
    stderr_chunks: list[str] = []

    def _drain_stdout() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            stdout_chunks.append(line)
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

    try:
        # 1. Send initialize and wait for the id=1 reply.
        proc.stdin.write(json.dumps(init_frame) + "\n")
        proc.stdin.flush()
        init_reply = _read_until_id(stdout_q, target_id=1, timeout=15)
        assert init_reply is not None, (
            f"Server never sent initialize reply (id=1).\nstderr so far: {''.join(stderr_chunks)!r}"
        )

        # 2. Send initialized notification then tools/list.
        proc.stdin.write(json.dumps(initialized_notif) + "\n")
        proc.stdin.flush()
        proc.stdin.write(json.dumps(tools_list_frame) + "\n")
        proc.stdin.flush()

        tools_reply = _read_until_id(stdout_q, target_id=2, timeout=15)
        assert tools_reply is not None, (
            f"Server never sent tools/list reply (id=2).\nstderr so far: {''.join(stderr_chunks)!r}"
        )

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

    stderr = "".join(stderr_chunks)
    stdout = "".join(stdout_chunks)

    # Assertion 1: deprecation warning present in stderr.
    assert "DeprecationWarning" in stderr, (
        f"`python -m nora` must emit DeprecationWarning on stderr; got: {stderr!r}"
    )
    assert "will be removed in the next minor release" in stderr, (
        f"DeprecationWarning must mention the removal version; got: {stderr!r}"
    )

    # Assertion 2: parse tools/list reply to confirm the nine-tool surface.
    tools: list[dict] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload_obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload_obj.get("id") == 2 and "result" in payload_obj:
            tools = payload_obj["result"].get("tools", [])
            break

    tool_names = {t.get("name") for t in tools}
    expected = {
        "snmp_get_pmp450i_radio_metrics",
        "snmp_get_ap_summary",
        "snmp_get_frame_utilization",
        "snmp_get_sm_table",
        "snmp_get_sm_detailed_diagnostics",
        "search_intervention_history",
        "get_device_lifecycle_summary",
        "correlate_sector_interference",
        "save_intervention_record",
    }
    assert tool_names == expected, (
        f"`python -m nora` must expose the same 9 tools as nora-mcp; got {tool_names}"
    )
