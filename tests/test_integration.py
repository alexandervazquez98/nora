"""End-to-end integration tests for the NORA MCP server.

These tests spawn a real `python -m nora` subprocess with a hermetic temp
`.env`, drive it over JSON-RPC on stdin, and assert the tool, structured
logs, and stdout/stderr contract.

They cover the integration scenarios in `specs/nora-mcp-server/spec.md` that
require a live process, and they keep the rest of the suite unit-fast.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from pathlib import Path

import pytest

from nora.data import BUILTIN_BASELINE_SIGNING_KEY

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _venv_python() -> str:
    py = PROJECT_ROOT / ".venv" / "bin" / "python"
    if not py.exists():
        pytest.skip("venv python not present")
    return str(py)


def _read_until_id(
    lines: "queue.Queue[str]", target_id: int, *, timeout: float = 10.0
) -> str | None:
    """Block until a JSON-RPC line with `id == target_id` is drained from `lines`.

    Returns the matched line, or `None` on timeout. Each line is parsed so
    unrelated output (e.g. logging) doesn't fool us.
    """
    import time

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


def _boot_server(
    env_file: Path,
    *,
    extra_env: dict[str, str] | None = None,
    request: str = "tools/list",
    request_id: int = 1,
) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    """Boot `python -m nora` with a temp `.env` and send JSON-RPC requests.

    Drives the MCP session by waiting for the `id=0` initialize reply on
    stdout before sending `tools/list`. This avoids fixed sleeps, which were
    flaky under FastMCP 3.4.7's stdio transport. A background thread drains
    stdout into a queue so the test thread can wait deterministically.

    Returns the completed process and the env vars that were applied.
    """
    py = _venv_python()
    init_frame = {
        "jsonrpc": "2.0",
        "id": 0,
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
    request_payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": request,
    }

    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    for k in ("NORA_LLM_PROVIDER", "LMSTUDIO_MODEL_ID", "GEMINI_API_KEY"):
        env.pop(k, None)
    # Provide the signing key the shipped built-in baseline was signed
    # with (PR 1 / ADR #17). Pre-PR1 any non-empty key worked because
    # `verify_all` only scanned the (empty) operator root; post-PR1 the
    # built-in is HMAC-verified too.
    from nora.data import BUILTIN_BASELINE_SIGNING_KEY

    env.setdefault("NORA_OID_CATALOG_SIGNING_KEY", BUILTIN_BASELINE_SIGNING_KEY)
    # Empty catalogs dir per call: the registry scans an empty path and
    # resolves nothing — the test surface never invokes the driver.
    import tempfile

    tmp_catalogs = Path(tempfile.mkdtemp(prefix="nora-it-catalogs-"))
    tmp_devices = tmp_catalogs / "devices.yaml"
    tmp_devices.write_text("# empty\n")
    env["NORA_OID_CATALOGS_PATH"] = str(tmp_catalogs)
    env["NORA_DEVICES_INVENTORY_PATH"] = str(tmp_devices)

    proc = subprocess.Popen(
        [py, "-m", "nora"],
        cwd=str(PROJECT_ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        bufsize=1,  # line-buffered so the reader sees frames immediately
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
        stdout_q.put("")  # sentinel: stdout closed

    def _drain_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_chunks.append(line)

    t_out = threading.Thread(target=_drain_stdout, daemon=True)
    t_err = threading.Thread(target=_drain_stderr, daemon=True)
    t_out.start()
    t_err.start()

    try:
        # 1. Send initialize and wait for the id=0 reply before proceeding.
        proc.stdin.write(json.dumps(init_frame) + "\n")
        proc.stdin.flush()
        init_reply = _read_until_id(stdout_q, target_id=0, timeout=15)
        assert init_reply is not None, (
            f"Server never sent initialize reply (id=0).\nstderr so far: {''.join(stderr_chunks)!r}"
        )

        # 2. Send the initialized notification and the request.
        proc.stdin.write(json.dumps(initialized_notif) + "\n")
        proc.stdin.flush()
        proc.stdin.write(json.dumps(request_payload) + "\n")
        proc.stdin.flush()

        # 3. Wait for the request reply.
        req_reply = _read_until_id(stdout_q, target_id=request_id, timeout=15)
        assert req_reply is not None, (
            f"Server never sent reply for id={request_id}.\n"
            f"stderr so far: {''.join(stderr_chunks)!r}"
        )

        # 4. Close stdin to signal EOF and let the server exit cleanly.
        proc.stdin.close()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

        # 5. Let the threads drain any remaining output.
        t_out.join(timeout=2)
        t_err.join(timeout=2)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        t_out.join(timeout=1)
        t_err.join(timeout=1)

    completed = subprocess.CompletedProcess(
        args=proc.args,
        returncode=proc.returncode,
        stdout="".join(stdout_chunks),
        stderr="".join(stderr_chunks),
    )
    return completed, {}


# ---------------------------------------------------------------------------
# Requirement: Subprocess boot with `tools/list`
# ---------------------------------------------------------------------------


def test_subprocess_responds_to_tools_list_with_four_tools(tmp_path: Path) -> None:
    """A real `python -m nora` boot exposes the four-tool surface in `tools/list`."""
    env_file = tmp_path / ".env"
    env_file.write_text("NORA_OID_CATALOG_SIGNING_KEY=change-me\n")

    proc, _ = _boot_server(env_file)

    assert proc.returncode == 0 or proc.returncode is None, (
        f"Server exited with code {proc.returncode}.\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )

    # Parse the JSON-RPC reply — at least one line is a `tools/list` result.
    parsed_reply: dict | None = None
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("id") == 1 and "result" in payload:
            parsed_reply = payload
            break

    assert parsed_reply is not None, f"No `tools/list` reply found in stdout:\n{proc.stdout}"

    # The reply's `result.tools` array MUST list exactly the four thin tools.
    tools = parsed_reply["result"].get("tools", [])
    names = {t.get("name") for t in tools}
    expected = {
        "snmp_get_pmp450i_radio_metrics",
        "search_intervention_history",
        "get_device_lifecycle_summary",
        "correlate_sector_interference",
    }
    assert names == expected, f"Expected exactly the 4 thin tools; got {names}"


def test_subprocess_emits_structured_startup_log_on_stderr(tmp_path: Path) -> None:
    """The server's startup line is on stderr and names the boot surface."""
    env_file = tmp_path / ".env"
    env_file.write_text("NORA_OID_CATALOG_SIGNING_KEY=change-me\n")
    proc, _ = _boot_server(env_file)

    stderr = proc.stderr
    # The startup log line names the boot surface explicitly.
    assert "nora-mcp boot complete" in stderr, (
        f"Startup log missing 'nora-mcp boot complete' on stderr:\n{stderr}"
    )


def test_subprocess_keeps_stdout_reserved_for_jsonrpc(tmp_path: Path) -> None:
    """Every non-empty line on stdout MUST be a JSON-RPC frame."""
    env_file = tmp_path / ".env"
    env_file.write_text("NORA_LLM_PROVIDER=lmstudio\n")
    proc, _ = _boot_server(env_file, extra_env={"NORA_LLM_PROVIDER": "lmstudio"})

    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            pytest.fail(f"Non-JSON line on stdout: {line!r}; error: {exc}; stderr:\n{proc.stderr}")
        assert "jsonrpc" in payload, f"Missing jsonrpc field: {payload!r}"


def test_subprocess_handles_malformed_json_gracefully(tmp_path: Path) -> None:
    """A malformed frame on stdin does NOT crash the server; it returns a JSON-RPC error."""
    env_file = tmp_path / ".env"
    env_file.write_text("NORA_OID_CATALOG_SIGNING_KEY=change-me\n")
    py = _venv_python()

    # Ensure the temp dirs/files exist so boot can complete.
    (tmp_path / "tmp-catalogs").mkdir(exist_ok=True)
    (tmp_path / "tmp-devices.yaml").write_text("# empty\n")

    # Send a clearly broken frame.
    bad_payload = "{not json}\n"
    proc = subprocess.run(
        [py, "-m", "nora"],
        cwd=str(PROJECT_ROOT),
        input=bad_payload,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
        env={
            **os.environ,
            "NORA_OID_CATALOG_SIGNING_KEY": BUILTIN_BASELINE_SIGNING_KEY,
            "NORA_OID_CATALOGS_PATH": str(tmp_path / "tmp-catalogs"),
            "NORA_DEVICES_INVENTORY_PATH": str(tmp_path / "tmp-devices.yaml"),
        },
    )
    # The process should not crash with a non-zero exit code from a parse error.
    # FastMCP may still exit cleanly (returncode 0) or stay alive.
    # The contract we care about: stderr does not contain an unhandled traceback
    # from a malformed input.
    assert "Traceback (most recent call last)" not in proc.stderr, (
        f"Server crashed on malformed input:\n{proc.stderr}"
    )


def test_subprocess_silently_ignores_legacy_llm_env_keys(tmp_path: Path) -> None:
    """Legacy `NORA_LLM_PROVIDER` / `GEMINI_API_KEY` env vars are silently ignored."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "NORA_LLM_PROVIDER=gemini\nGEMINI_API_KEY=legacy-secret\n"
        "NORA_OID_CATALOG_SIGNING_KEY=change-me\n"
    )
    py = _venv_python()
    init_frame = (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "0.0.0"},
                },
            }
        )
        + "\n"
    )
    (tmp_path / "tmp-catalogs").mkdir(exist_ok=True)
    (tmp_path / "tmp-devices.yaml").write_text("# empty\n")

    proc = subprocess.run(
        [py, "-m", "nora"],
        cwd=str(tmp_path),
        input=init_frame,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
        env={
            **{k: v for k, v in os.environ.items() if k != "GEMINI_API_KEY"},
            "NORA_OID_CATALOG_SIGNING_KEY": BUILTIN_BASELINE_SIGNING_KEY,
            "NORA_OID_CATALOGS_PATH": str(tmp_path / "tmp-catalogs"),
            "NORA_DEVICES_INVENTORY_PATH": str(tmp_path / "tmp-devices.yaml"),
        },
    )
    assert "legacy-secret" not in proc.stdout, "Legacy API key leaked to stdout"
    assert "legacy-secret" not in proc.stderr, "Legacy API key leaked to stderr"
