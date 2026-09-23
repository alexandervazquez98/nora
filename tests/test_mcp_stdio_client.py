"""Unit tests for McpStdioClient — JSON-RPC over newline-delimited stdio.

These tests verify the client wrapper in isolation by spawning a tiny
in-process JSON-RPC echo server. They do NOT touch the real `nora-mcp`
binary; integration with `nora-mcp` is covered separately by the
migrated integration tests (WU-4 / WU-5 of feat/test-perf-stdio-fixture).

Why a custom echo server: the MCP stdio protocol is just JSON-RPC 2.0 over
newline-delimited JSON. Anything that reads a JSON line, dispatches by
`method`, and writes a JSON response line is a valid MCP-shaped server for
transport-level tests. This keeps the client's behavior verifiable without
paying the cost of booting `nora-mcp` (~1.3 s) per assertion.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

from tests.conftest import McpStdioClient

_ECHO_SERVER = textwrap.dedent(
    """
    import sys, json

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        # Requests have an `id`; notifications do not. The real MCP server
        # only sends a response line for requests. Mirror that.
        if "id" in msg:
            response = {
                "jsonrpc": "2.0",
                "id": msg["id"],
                "result": {"echo": msg.get("method"), "params": msg.get("params", {})},
            }
            sys.stdout.write(json.dumps(response) + "\\n")
            sys.stdout.flush()
    """
).strip()


def _spawn_echo_server() -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-c", _ECHO_SERVER],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )


def test_stdio_client_initialize_round_trips() -> None:
    """`initialize` request gets a response with the same `id` and the echoed method."""
    proc = _spawn_echo_server()
    try:
        client = McpStdioClient(proc)
        response = client.initialize()

        assert response["jsonrpc"] == "2.0"
        assert response["id"] == 1
        assert response["result"]["echo"] == "initialize"
    finally:
        proc.terminate()
        proc.wait()


def test_stdio_client_tools_list_round_trips() -> None:
    """`tools/list` request gets a response with the same `id`."""
    proc = _spawn_echo_server()
    try:
        client = McpStdioClient(proc)
        response = client.tools_list()

        assert response["jsonrpc"] == "2.0"
        assert response["id"] == 2
        assert response["result"]["echo"] == "tools/list"
    finally:
        proc.terminate()
        proc.wait()


def test_stdio_client_initialized_notification_does_not_block() -> None:
    """`initialized` is a notification (no `id`); the server does not reply."""
    # Use a stricter echo that closes stdout after sending ONE response —
    # if the client waits for a second response line on a notification, the
    # test hangs (or fails the readline on EOF).
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                """
                import sys, json
                # Read one line, send one response. If the client sends more,
                # the server keeps replying (it's the echo).
                # We then close stdout to simulate a notification-style
                # server that doesn't reply to `initialized`.
                for line in sys.stdin:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if msg.get("method") == "notifications/initialized":
                        # Notification: server does NOT respond, just keeps reading.
                        continue
                    if "id" in msg:
                        sys.stdout.write(
                            json.dumps({
                                "jsonrpc": "2.0",
                                "id": msg["id"],
                                "result": {"echo": msg.get("method")},
                            }) + "\\n"
                        )
                        sys.stdout.flush()
                """
            ).strip(),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    try:
        client = McpStdioClient(proc)
        # First a request, to prove the client works.
        result = client.request("tools/list", id=42)
        assert result["result"]["echo"] == "tools/list"
        # Now the notification — must not block on a non-existent reply.
        client.initialized()
    finally:
        proc.terminate()
        proc.wait()


def test_stdio_client_raises_on_closed_stdout() -> None:
    """If the server closes stdout, `request` raises instead of returning empty data."""
    # A server that accepts one line then closes stdout — the client's next
    # request must raise rather than silently returning an empty response.
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                """
                import sys, json
                line = sys.stdin.readline()
                if not line:
                    sys.exit(0)
                msg = json.loads(line.strip())
                if "id" in msg:
                    sys.stdout.write(json.dumps({
                        "jsonrpc": "2.0",
                        "id": msg["id"],
                        "result": {"ok": True},
                    }) + "\\n")
                    sys.stdout.flush()
                sys.stdout.close()
                sys.exit(0)
                """
            ).strip(),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    try:
        client = McpStdioClient(proc)
        # First request gets a response.
        result = client.request("tools/list", id=1)
        assert result["result"]["ok"] is True
        # Server has now closed stdout — second request must raise.
        import pytest

        with pytest.raises(RuntimeError, match="stdout"):
            client.request("tools/list", id=2)
    finally:
        proc.terminate()
        proc.wait()
