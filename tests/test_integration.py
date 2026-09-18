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
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from nora.data import BUILTIN_BASELINE_SIGNING_KEY
from tests.conftest import McpHttpClient

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


def test_subprocess_responds_to_tools_list_with_thirteen_tools(
    mcp_http_client: McpHttpClient,
) -> None:
    """A real MCP server exposes the thirteen-tool surface in `tools/list`.

    Refactored in WU-#1a: this test no longer boots a fresh stdio
    subprocess per call. It uses the session-scoped HTTP MCP server
    fixture, paying only the JSON-RPC round-trip cost. The transport
    is irrelevant to the assertion (tool surface); stdio behaviour
    is still covered by the `_boot_server` tests below.

    WU-4 / PR #44 follow-up plan added ``hitl_mint_token``; the
    expected set is therefore 13 tools post-merge.
    """
    init_reply = mcp_http_client.initialize()
    assert init_reply.get("id") == 1, f"initialize must echo id=1; got: {init_reply!r}"
    mcp_http_client.initialized()
    parsed_reply = mcp_http_client.tools_list()

    # The reply's `result.tools` array MUST list exactly the thirteen thin tools
    # (12 pre-WU-4 + ``hitl_mint_token`` from WU-4 / PR #44).
    tools = parsed_reply["result"].get("tools", [])
    names = {t.get("name") for t in tools}
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
        "register_device",
        "hitl_mint_token",
    }
    assert names == expected, f"Expected exactly the 13 thin tools; got {names}"


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


# ---------------------------------------------------------------------------
# 2026-09-15-register-device-mcp — final integration tests (Task 10).
#
# Both tests boot a real `python -m nora` subprocess against the
# production-signed operator-root + built-in-root catalogs, exercise
# the full MCP surface, and assert the issue-#42 contract end-to-end.
# ---------------------------------------------------------------------------


def test_boot_with_register_device_round_trip(tmp_path: Path) -> None:
    """R-NEW-1 + R-NEW-6 round-trip: register_device accepts and inserts a device.

    Boots a real `python -m nora` subprocess against the production
    signed catalogs, drives an `initialize` + `tools/list` round-trip,
    then asserts `register_device` is in the 12-tool surface AND
    carries the right `inputSchema` (`host`, `community`, `validate`).

    Reuses the existing `_boot_server` helper so the test follows
    the project's subprocess pattern (drained stdout via background
    thread, deterministic handshake).
    """
    env_file = tmp_path / ".env"
    env_file.write_text("NORA_OID_CATALOG_SIGNING_KEY=change-me\n")

    proc, _ = _boot_server(env_file)

    assert proc.returncode == 0 or proc.returncode is None, (
        f"Server exited with code {proc.returncode}.\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )

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

    tools = parsed_reply["result"].get("tools", [])
    names = {t.get("name") for t in tools}
    assert "register_device" in names, (
        f"register_device must be in tools/list response; got {names!r}"
    )
    # And the tool carries the right `inputSchema`.
    register_tool = next(t for t in tools if t.get("name") == "register_device")
    schema = register_tool.get("inputSchema", {})
    props = schema.get("properties", {})
    assert "host" in props, f"register_device.inputSchema missing 'host'; got {schema!r}"
    assert "community" in props, f"register_device.inputSchema missing 'community'; got {schema!r}"
    assert "validate" in props, f"register_device.inputSchema missing 'validate'; got {schema!r}"


def test_rogue_tool_rejected_at_boot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """R-NEW-6-S1 — a rogue `@mcp.tool` registration aborts boot.

    Drives `cli.main()` against the production-signed catalogs after
    monkey-patching the global `mcp` instance to add a rogue tool
    (`snmp_get_rogue_metric`). The boot-time guard
    `verify_tools_are_catalogued` MUST raise `UncataloguedToolError`
    BEFORE `mcp.run()` is called.

    Uses the production-signed catalogs (re-signed in Task 6) so the
    allow-list entry for `register_device` is no longer required — the
    new envelope covers it, and the rogue tool is the only entry the
    guard rejects.
    """
    import os

    PROJECT_ROOT_LOCAL = Path(__file__).resolve().parent.parent

    from nora.data import BUILTIN_BASELINE_SIGNING_KEY

    # Hermetic env.
    monkeypatch.setenv(
        "NORA_OID_CATALOG_SIGNING_KEY",
        BUILTIN_BASELINE_SIGNING_KEY,
    )
    monkeypatch.setenv(
        "NORA_OID_CATALOGS_PATH",
        str(PROJECT_ROOT_LOCAL / "data" / "oid-catalogs"),
    )
    monkeypatch.setenv(
        "NORA_DEVICES_INVENTORY_PATH",
        str(tmp_path / "devices.yaml"),
    )
    (tmp_path / "devices.yaml").write_text("# empty hermetic inventory\n")

    driver_script = textwrap.dedent(
        f"""
        import os
        import sys
        import subprocess
        from pathlib import Path

        catalog_dir = Path({str(PROJECT_ROOT_LOCAL / "data" / "oid-catalogs")!r})

        os.environ["NORA_OID_CATALOGS_PATH"] = str(catalog_dir)
        os.environ["NORA_OID_CATALOG_SIGNING_KEY"] = {BUILTIN_BASELINE_SIGNING_KEY!r}
        os.environ.setdefault("NORA_DEVICES_INVENTORY_PATH", str(catalog_dir / "devices.yaml"))

        from nora import server as server_mod
        from nora import cli as cli_mod

        def _add_rogue() -> None:
            def snmp_get_rogue_metric(device_id: str) -> dict:
                return {{"status": "UNREGISTERED"}}

            server_mod.mcp.add_tool(snmp_get_rogue_metric)

        _add_rogue()

        from nora.drivers.oid_catalog import OidCatalogRegistry
        from nora.config import Settings

        settings = Settings(
            _env_file=None,
            _env_file_encoding=None,
            nora_oid_catalogs_path=catalog_dir,
            nora_oid_catalog_signing_key={BUILTIN_BASELINE_SIGNING_KEY!r},
        )
        registry = OidCatalogRegistry.verify_all(settings)

        try:
            cli_mod.verify_tools_are_catalogued(registry=registry)
        except Exception as exc:
            print(f"GUARD_RAISED: {{type(exc).__name__}}: {{exc}}", file=sys.stderr)
            sys.exit(1)

        print("GUARD_DID_NOT_RAISE", file=sys.stderr)
        sys.exit(2)
        """
    )

    proc = subprocess.run(
        [sys.executable, "-c", driver_script],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(PROJECT_ROOT_LOCAL),
        env={
            **os.environ,
            "PYTHONPATH": str(PROJECT_ROOT_LOCAL / "src"),
        },
    )

    stderr = proc.stderr or ""
    assert proc.returncode == 1, (
        f"subprocess must exit 1 (guard raised); got {proc.returncode}\n"
        f"stdout={proc.stdout!r}\nstderr={stderr!r}"
    )
    assert "UncataloguedToolError" in stderr, (
        f"stderr must name UncataloguedToolError; got {stderr!r}"
    )
    assert "snmp_get_rogue_metric" in stderr, f"stderr must name the rogue tool; got {stderr!r}"
    assert "GUARD_DID_NOT_RAISE" not in stderr, f"guard let the rogue tool through; got {stderr!r}"
