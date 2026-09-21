"""W6 stdio smoke test — `save_intervention_record` lands via FastMCP stdio.

Boots the server as a real subprocess, drives the MCP handshake over
JSON-RPC, calls `save_intervention_record` with a valid payload, and
asserts the resulting `.json` lands under the configured
`NORA_INTERVENTIONS_DIR`.

Why this test uses an inline subprocess and NOT the session-scoped
`mcp_stdio_server` fixture (`tests/conftest.py`):

- Per-test `NORA_INTERVENTIONS_DIR`: this test creates a fresh
  `tempfile.mkdtemp(prefix="nora-writer-smoke-")` and asserts
  `len(json_files) == 1`. Sharing the fixture would mean all tests
  in the same worker write to the same directory, breaking the
  "exactly one INT-*.json on disk" assertion.
- Per-boot env-var injection: the env block includes a per-test
  `NORA_INTERVENTIONS_DIR`; the session-scoped fixture captures
  its env once at boot and cannot be re-configured per test
  without losing the shared-boot benefit.

See `odd/tasks/test-perf-stdio-fixture.md` WU-6 for the full rationale
and the list of stdio-specific tests that intentionally remain inline.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import tempfile
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _venv_python() -> str:
    py = PROJECT_ROOT / ".venv" / "bin" / "python"
    if not py.exists():
        import pytest

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


def test_save_intervention_record_lands_via_stdio() -> None:
    """A real `python -m nora` boot writes the record when called over stdio."""
    from nora.data import BUILTIN_BASELINE_SIGNING_KEY

    py = _venv_python()
    interventions_dir = Path(tempfile.mkdtemp(prefix="nora-writer-smoke-"))
    catalogs_dir = interventions_dir / "catalogs"
    catalogs_dir.mkdir()
    devices_yaml = interventions_dir / "devices.yaml"
    devices_yaml.write_text("# empty\n")

    env = {
        **os.environ,
        "NORA_OID_CATALOG_SIGNING_KEY": BUILTIN_BASELINE_SIGNING_KEY,
        "NORA_OID_CATALOGS_PATH": str(catalogs_dir),
        "NORA_DEVICES_INVENTORY_PATH": str(devices_yaml),
        "NORA_INTERVENTIONS_DIR": str(interventions_dir),
    }

    # Handshake + tools/list + tools/call (save_intervention_record).
    init_frame = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "writer-smoke", "version": "0.0.0"},
        },
    }
    initialized_notif = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    call_frame = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
            "name": "save_intervention_record",
            "arguments": {
                "payload": {
                    "intervention_id": "INT-SMOKE-001-10.0.0.1-1700000000-abcdef",
                    "timestamp_iso": "2026-01-01T12:00:00+00:00",
                    "timestamp_unix": 1700000000,
                    "ticket_number": "SMOKE-001",
                    "target_ip": "10.0.0.1",
                    "stage": "PRE_DIAGNOSTIC",
                    "record_name": "Smoke test record",
                    "status": "COMPLETED",
                    "agent_name": "writer-smoke",
                    "findings_and_dictamen": "smoke ok",
                    "created_at": "2026-01-01T12:00:00+00:00",
                    "network_equipment": {},
                }
            },
        },
    }

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

    try:
        # 1. Initialize
        proc.stdin.write(json.dumps(init_frame) + "\n")
        proc.stdin.flush()
        init_reply = _read_until_id(stdout_q, target_id=1, timeout=15)
        assert init_reply is not None, (
            f"Server never sent initialize reply.\nstderr: {''.join(stderr_chunks)!r}"
        )

        # 2. Initialized notification + tools/call.
        proc.stdin.write(json.dumps(initialized_notif) + "\n")
        proc.stdin.flush()
        proc.stdin.write(json.dumps(call_frame) + "\n")
        proc.stdin.flush()

        # 3. Wait for the call reply.
        call_reply = _read_until_id(stdout_q, target_id=3, timeout=15)
        assert call_reply is not None, (
            f"Server never replied to save_intervention_record.\nstderr: {''.join(stderr_chunks)!r}"
        )

        proc.stdin.close()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        t_out.join(timeout=1)
        t_err.join(timeout=1)

    # Parse the call reply — must include `status: "OK"`.
    parsed = json.loads(call_reply)
    assert "result" in parsed, f"No `result` in call reply: {parsed}"
    call_payload = parsed["result"]
    # FastMCP returns the dict wrapped — assert it's OK.
    if isinstance(call_payload, dict) and "structuredContent" in call_payload:
        result_dict = call_payload["structuredContent"]
    elif isinstance(call_payload, dict) and "content" in call_payload:
        # Extract from text content
        text = call_payload["content"][0].get("text", "{}")
        result_dict = json.loads(text)
    else:
        result_dict = call_payload
    assert result_dict.get("status") == "OK", f"Expected OK from stdio call; got: {result_dict}"

    # The on-disk `.json` must exist under `interventions_dir`.
    json_files = sorted(interventions_dir.glob("INT-*.json"))
    assert len(json_files) == 1, (
        f"Expected exactly one INT-*.json on disk; got: "
        f"{[p.name for p in interventions_dir.iterdir()]}"
    )
    on_disk = json.loads(json_files[0].read_text())
    assert on_disk["ticket_number"] == "SMOKE-001"
    assert on_disk["target_ip"] == "10.0.0.1"
    assert on_disk["stage"] == "PRE_DIAGNOSTIC"
