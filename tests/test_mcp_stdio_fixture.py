"""Integration tests for the mcp_stdio_server session-scoped fixture.

Validates the fixture's lifecycle: boot, yield, cleanup. The fixture
shares ONE `nora-mcp` subprocess across multiple tests in the same
worker; this test verifies that:

1. The fixture yields a usable subprocess (alive, stdin/stdout pipes open)
2. JSON-RPC initialize round-trips through McpStdioClient
3. tools/list returns the expected NORA tool surface

These tests run against the REAL `nora-mcp` binary, so they take ~1.3s
per test for boot. That's the cost we're paying once per worker instead
of once per test.
"""

from __future__ import annotations

import pytest

from tests.conftest import McpStdioClient


def test_stdio_server_fixture_yields_alive_process(mcp_stdio_server):
    """The fixture yields a running nora-mcp subprocess with open pipes."""
    proc = mcp_stdio_server
    assert proc.poll() is None, "nora-mcp exited prematurely"
    assert proc.stdin is not None and proc.stdin.writable()
    assert proc.stdout is not None and proc.stdout.readable()


def test_stdio_server_fixture_handles_initialize(mcp_stdio_server):
    """JSON-RPC initialize round-trips through the fixture's subprocess."""
    client = McpStdioClient(mcp_stdio_server)
    response = client.initialize()

    assert response["jsonrpc"] == "2.0"
    assert response["id"] == 1
    assert "result" in response
    assert "serverInfo" in response["result"]


def test_stdio_server_fixture_lists_tools(mcp_stdio_server):
    """tools/list returns the NORA tool surface."""
    client = McpStdioClient(mcp_stdio_server)
    client.initialize()
    client.initialized()
    response = client.tools_list()

    assert response["jsonrpc"] == "2.0"
    assert "result" in response
    tools = response["result"]["tools"]
    # NORA exposes 13 MCP tools (4 effect + 9 read-only/prompts).
    # See src/nora/server.py for the authoritative count.
    assert len(tools) >= 13, f"Expected >=13 tools, got {len(tools)}"
    tool_names = {t["name"] for t in tools}
    # Spot-check a few well-known tools.
    assert "snmp_get_sm_table" in tool_names
    assert "nora_get_tool_spec" in tool_names
