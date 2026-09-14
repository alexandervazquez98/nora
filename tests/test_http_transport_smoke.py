"""HTTP transport smoke test.

Boots `nora-mcp --transport=http --host=127.0.0.1 --port=8765 --path=/mcp`
as a real subprocess (not in-process) and probes `/mcp` with httpx. Any
non-zero HTTP status (the GET itself returns `405 Method Not Allowed` or
similar — FastMCP only handles POST on `/mcp`) is sufficient evidence
that the server bound the socket and is serving the MCP protocol. This
mirrors the existing `tests/test_main_alias.py` stdio-subprocess pattern.

Port 8765 is chosen to avoid the documented default `8005` so the test
does not conflict with a real `nora-mcp` systemd deployment.

Issue #34: closes the loop on the HTTP smoke-test gap noted in
`design.md` "Test Strategy → Integration".
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BIND_HOST = "127.0.0.1"
BIND_PORT = 8765
BIND_PATH = "/mcp"


def _venv_python() -> str:
    py = PROJECT_ROOT / ".venv" / "bin" / "python"
    if not py.exists():
        pytest.skip("venv python not present")
    return str(py)


def _port_is_open(host: str, port: int, timeout: float = 0.5) -> bool:
    """True if `host:port` accepts a TCP connection within `timeout`."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@pytest.fixture(scope="module")
def http_proc(tmp_path_factory: pytest.TempPathFactory) -> "subprocess.Popen[bytes]":
    """Boot `nora-mcp` on `127.0.0.1:8765/mcp` and tear down after the test."""
    py = _venv_python()
    # PR 1 (ADR #17) signing key — the built-in baseline catalog at
    # `src/nora/data/oid-catalogs/` was signed with this key, so it MUST
    # be in the child env or `OidCatalogRegistry.verify_all` aborts boot.
    from nora.data import BUILTIN_BASELINE_SIGNING_KEY

    tmp = tmp_path_factory.mktemp("http_smoke")
    (tmp / "catalogs").mkdir(exist_ok=True)
    (tmp / "devices.yaml").write_text("# empty hermetic inventory\n")

    env = {
        **os.environ,
        "NORA_OID_CATALOG_SIGNING_KEY": BUILTIN_BASELINE_SIGNING_KEY,
        "NORA_OID_CATALOGS_PATH": str(tmp / "catalogs"),
        "NORA_DEVICES_INVENTORY_PATH": str(tmp / "devices.yaml"),
        "NORA_MCP_TRANSPORT": "http",
        "NORA_MCP_HOST": BIND_HOST,
        "NORA_MCP_PORT": str(BIND_PORT),
        "NORA_MCP_PATH": BIND_PATH,
        # Disable any prior stdout JSON-RPC framing — HTTP is a separate
        # surface from stdio.
    }

    proc = subprocess.Popen(
        [
            py,
            "-m",
            "nora.cli",
            "--transport=http",
            f"--host={BIND_HOST}",
            f"--port={BIND_PORT}",
            f"--path={BIND_PATH}",
        ],
        cwd=str(PROJECT_ROOT),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        bufsize=1,
    )

    # Wait up to 15 seconds for the socket to bind. The boot log line
    # "Starting MCP server 'nora' with transport 'http'" is the readiness
    # signal but we don't parse stderr here — `socket.create_connection`
    # is faster and more reliable than log scraping.
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            err = b""
            try:
                assert proc.stderr is not None
                err = proc.stderr.read() or b""
            except Exception:
                pass
            pytest.fail(
                f"nora-mcp exited before bind (rc={proc.returncode}); "
                f"stderr: {err.decode(errors='replace')!r}"
            )
        if _port_is_open(BIND_HOST, BIND_PORT, timeout=0.2):
            break
        time.sleep(0.1)
    else:
        proc.kill()
        proc.wait()
        pytest.fail(f"nora-mcp never bound {BIND_HOST}:{BIND_PORT} within 15s")

    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        # Drain remaining stderr so the test runner doesn't see a
        # broken-pipe warning at the next stream read.
        try:
            assert proc.stderr is not None
            proc.stderr.read()
        except Exception:
            pass


def test_http_smoke_root_returns_any_status(http_proc: "subprocess.Popen[bytes]") -> None:
    """`GET /mcp` returns ANY non-zero HTTP status — proves the socket is bound."""
    import httpx

    # GET on `/mcp` is not a method FastMCP handles. FastMCP returns 405
    # Method Not Allowed (or a similar 4xx). The point of this test is
    # that an HTTP response comes back at all — a connection refused
    # would mean the socket never bound.
    response = httpx.get(
        f"http://{BIND_HOST}:{BIND_PORT}{BIND_PATH}",
        timeout=3.0,
    )
    assert response.status_code >= 200, (
        f"HTTP probe must return a valid status code; got {response.status_code}"
    )
    # Distinguish "server bound but rejected the method" (good) from
    # "no server at all" (bad). ECONNREFUSED surfaces as a
    # httpx.ConnectError before we get here, so the assertion below is
    # defensive — we should never see 0.
    assert response.status_code in (200, 307, 400, 404, 405, 406, 415), (
        f"unexpected HTTP status; FastMCP /mcp GET should return 405 or "
        f"similar, got {response.status_code}"
    )


def test_http_smoke_initialize_round_trip(http_proc: "subprocess.Popen[bytes]") -> None:
    """POST `/mcp` with an `initialize` JSON-RPC frame returns an `initialize` reply.

    This is the "the server speaks MCP over HTTP" smoke — same shape as
    the stdio `check_functional()` probe in `scripts/verify-install.sh`,
    just over Streamable-HTTP instead of stdin/stdout.
    """
    import httpx

    init_frame = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "pytest-http-smoke", "version": "0.0.0"},
        },
    }
    payload = json.dumps(init_frame).encode("utf-8") + b"\n"

    # Streamable HTTP expects either Content-Length framing or chunked
    # TE. Use `content=` so httpx sets Content-Length.
    response = httpx.post(
        f"http://{BIND_HOST}:{BIND_PORT}{BIND_PATH}",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        timeout=5.0,
    )
    # Acceptable shapes: 200 with a JSON body containing the initialize
    # reply, OR 202 (Streamable-HTTP may ack via SSE — but a single
    # initialize on its own is enough to land here, no SSE upgrade).
    assert response.status_code in (200, 202), (
        f"initialize POST must return 200/202; got {response.status_code}; body={response.text!r}"
    )
    # Streamable-HTTP may return text/event-stream; handle both shapes.
    if response.headers.get("content-type", "").startswith("application/json"):
        body = response.json()
    else:
        # text/event-stream: data: {...} line carries the payload
        data_line = next(
            (ln for ln in response.text.splitlines() if ln.startswith("data:")),
            None,
        )
        assert data_line is not None, (
            f"SSE response must contain a `data:` line; got: {response.text!r}"
        )
        body = json.loads(data_line.split(":", 1)[1].strip())

    assert body.get("id") == 1, f"initialize reply must echo id=1; got: {body!r}"
    assert "result" in body or "error" in body, (
        f"initialize reply must carry a result or error; got: {body!r}"
    )
