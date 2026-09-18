"""HTTP transport smoke test.

Boots `nora-mcp --transport=http` as a real subprocess (not in-process)
and probes `/mcp` with httpx. Any non-zero HTTP status (the GET itself
returns `405 Method Not Allowed` or similar — FastMCP only handles POST
on `/mcp`) is sufficient evidence that the server bound the socket and
is serving the MCP protocol.

Refactored in WU-#3: uses the session-scoped `mcp_http_server` fixture
from `tests/conftest.py` (one HTTP server per pytest-xdist worker, free
OS-allocated port). This:

- eliminates the per-test subprocess boot (paid ONCE per worker)
- removes the fixed `BIND_PORT = 8765` collision that made these tests
  flaky under pytest-xdist (the original `no_xdist` mark is no longer
  required — the per-worker free port allocation prevents it)

Mirrors the existing `tests/test_main_alias.py` stdio-subprocess pattern.

Issue #34: closes the loop on the HTTP smoke-test gap noted in
`design.md` "Test Strategy → Integration".
"""

from __future__ import annotations

import subprocess

from tests.conftest import McpHttpClient


def test_http_smoke_root_returns_any_status(
    mcp_http_server: tuple[str, int, subprocess.Popen[bytes]],
) -> None:
    """`GET /mcp` returns ANY non-zero HTTP status — proves the socket is bound."""
    import httpx

    host, port, _proc = mcp_http_server
    # GET on `/mcp` is not a method FastMCP handles. FastMCP returns 405
    # Method Not Allowed (or a similar 4xx). The point of this test is
    # that an HTTP response comes back at all — a connection refused
    # would mean the socket never bound.
    response = httpx.get(
        f"http://{host}:{port}/mcp",
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


def test_http_smoke_initialize_round_trip(
    mcp_http_client: McpHttpClient,
) -> None:
    """POST `/mcp` with an `initialize` JSON-RPC frame returns an `initialize` reply.

    This is the "the server speaks MCP over HTTP" smoke — same shape as
    the stdio `check_functional()` probe in `scripts/verify-install.sh`,
    just over Streamable-HTTP instead of stdin/stdout.
    """
    init_reply = mcp_http_client.initialize()
    assert init_reply.get("id") == 1, f"initialize reply must echo id=1; got: {init_reply!r}"
    assert "result" in init_reply or "error" in init_reply, (
        f"initialize reply must carry a result or error; got: {init_reply!r}"
    )
