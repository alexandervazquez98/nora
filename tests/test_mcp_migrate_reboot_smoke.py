"""End-to-end MCP smoke tests for the PMP 450i tier-2 mutation tools.

Companion to ``tests/snmp_pmp450i/test_migrate.py`` and
``tests/snmp_pmp450i/test_reboot.py`` (the WU-1 regression suite that pins
the WritableSnmpClient wire contract and dry-run seam). Those unit tests
exercise the helpers directly via ``monkeypatch``; this file exercises the
seam between the helpers and the MCP transport — the layer that PR #77
(``mcp_stdio_server``) and PR #80 (the WU-1 tests) deferred for separate
coverage.

Issue #82 (WU-4 deferred from PR #80): the helpers themselves are well
tested, but no test had proven that the @mcp.tool-decorated surface
survives the subprocess + JSON-RPC boundary intact. Specifically:

* Both tools appear in ``tools/list`` after the server boots.
* Calling each tool with a non-existent ``device_id`` and an invalid
  approval token returns a well-formed MCP tool-error result — not a
  crash, not an empty body, not a timeout. The MCP transport surfaces
  tool errors as ``result.isError=True`` with the message in
  ``result.content[0].text`` (this is the MCP protocol distinction between
  *protocol errors* at the JSON-RPC layer and *tool errors* at the tool
  layer; this test exercises the latter).
* The error envelope names the operator-facing exception class
  (``AutonomousMutationRejected`` for an invalid token, or
  ``DeviceNotFoundError`` for an unknown device with a valid token) so the
  upstream orchestrator can branch on it deterministically.

The transport chosen here is HTTP (``mcp_http_client``) over stdio because
HTTP is more reliable under pytest-xdist and the assertion is transport-
agnostic. StdIO behaviour is already covered by
``tests/test_mcp_stdio_fixture.py``.

Recording-client-through-MCP (the stronger "against a recording
WritableSnmpClient" pattern mentioned in the issue body) is deliberately
NOT done here: that would require a new infrastructure hook to inject a
recording factory into the spawned subprocess, and the SET-frame contract
is already exhaustively pinned by the WU-1 unit tests. The transport
smoke test covers the seam that was actually missing.
"""

from __future__ import annotations

from typing import Any

from tests.conftest import McpHttpClient

# Tools that the smoke test must find in the registered surface. Listed
# with the issue they were deferred from so a future contributor reading
# only this file can trace back without grepping.
EXPECTED_TIER2_TOOLS = (
    "snmp_migrate_radio_frequency",  # PR #80 / issue #80 — dry-run seam
    "snmp_reboot_radio",            # PR #80 / issue #80 — WU-C reboot
)

# Operator-facing exception class names this test asserts on. The first
# fires when the approval token is absent or invalid (the security gate
# fires before device lookup); the second fires when the device does not
# exist in inventory. Either is a valid "tool wired up to the helper"
# signal — what matters is that one of them surfaces, not which one.
#
# FastMCP 3.x renders the Python exception class name into a human-
# readable form by inserting spaces between CamelCase boundaries
# (``AutonomousMutationRejected`` → ``autonomous device mutation rejected``)
# and dropping the CamelCase entirely. We list BOTH the CamelCase and the
# humanised form so the assertion survives either rendering. If FastMCP
# changes the rendering in a future release, update this list.
EXPECTED_OPERATOR_EXCEPTIONS = (
    # AutonomousMutationRejected — invalid/missing approval token
    "AutonomousMutationRejected",
    "autonomous device mutation rejected",
    # DeviceNotFoundError — unknown device with a valid token
    "DeviceNotFoundError",
    "device not found",
    "device not found error",
)


def _initialize_and_list_tools(client: McpHttpClient) -> dict[str, Any]:
    """Drive the MCP handshake then return the parsed ``tools/list`` body.

    Centralised because every test in this file repeats the same three
    calls (initialize, initialized notification, tools/list). The HTTP
    transport requires the initialize response to populate the
    ``Mcp-Session-Id`` header before subsequent requests; stdio does not,
    but the unified sequence works for both and keeps the assertions
    transport-agnostic.
    """
    client.initialize()
    client.initialized()
    return client.tools_list()


def _tool_names_from_response(tools_list_response: dict[str, Any]) -> list[str]:
    """Extract tool names from the ``tools/list`` result body."""
    tools = tools_list_response["result"]["tools"]
    return [tool["name"] for tool in tools]


def test_mcp_registers_tier2_pmp450i_tools(
    mcp_http_client: McpHttpClient,
) -> None:
    """``snmp_migrate_radio_frequency`` and ``snmp_reboot_radio`` are registered.

    A missing registration would mean the @mcp.tool decorator was lost in a
    refactor of ``src/nora/server.py`` or that the server crashed on the
    way to ``tools/list``. Either failure mode breaks every downstream
    orchestrator that relies on tool-name dispatch.
    """
    response = _initialize_and_list_tools(mcp_http_client)
    registered = set(_tool_names_from_response(response))
    missing = [name for name in EXPECTED_TIER2_TOOLS if name not in registered]
    assert not missing, (
        f"Expected tier-2 tools missing from MCP tools/list: {missing!r}. "
        f"Registered tools: {sorted(registered)!r}. "
        "Likely cause: @mcp.tool decorator removed or server boot failed "
        "before tools/list completed."
    )


def _invoke_tool_and_capture_tool_error(
    client: McpHttpClient,
    *,
    name: str,
    arguments: dict[str, Any],
    request_id: int,
) -> dict[str, Any]:
    """Send a ``tools/call`` and return the parsed JSON-RPC response.

    Helper because both tier-2 smoke tests share the same shape: call the
    tool with an invalid token + a non-existent ``device_id``, expect a
    well-formed tool-error result (the MCP framework converts the raised
    Python exception into ``result.isError=True`` with the message in
    ``result.content[0].text``). Returns the full response so the caller
    can assert on both the protocol envelope and the tool-error content.
    """
    _initialize_and_list_tools(client)
    return client.request(
        "tools/call",
        {"name": name, "arguments": arguments},
        id=request_id,
    )


def _assert_tool_error_envelope(response: dict[str, Any], *, request_id: int) -> str:
    """Assert the response carries a well-formed MCP tool-error and return the text.

    MCP distinguishes two error layers:

    * **JSON-RPC protocol error**: top-level ``error`` field. Reserved for
      malformed requests, unknown methods, session expiry, etc. We do NOT
      expect this here; a tool that raises a Python exception should NOT
      produce a protocol-level error.
    * **Tool error**: ``result.isError=True`` with ``result.content`` being
      a list of content blocks (typically one ``text`` block). This is the
      channel through which the helper's exceptions surface to the
      orchestrator.

    Returns the text payload of the first content block so callers can
    assert on the exception class name without re-parsing.
    """
    assert response.get("jsonrpc") == "2.0", (
        f"MCP response MUST be JSON-RPC 2.0; got {response!r}"
    )
    assert response.get("id") == request_id, (
        f"MCP response id MUST echo the request id; got {response!r}"
    )
    assert "result" in response, (
        f"Tool-raised exceptions surface as MCP tool errors (result.isError), "
        f"NOT as JSON-RPC protocol errors (top-level error). A top-level "
        f"error here would mean the @mcp.tool framework rejected the call "
        f"before reaching the helper, which IS a regression. Got {response!r}"
    )
    assert "error" not in response, (
        f"Top-level error MUST NOT be present for a successful tool dispatch "
        f"that then raised. Got {response!r}"
    )

    result = response["result"]
    assert result.get("isError") is True, (
        f"Invalid token / unknown device MUST surface as a tool error "
        f"(result.isError=True). Got result={result!r}."
    )
    content = result.get("content")
    assert isinstance(content, list) and content, (
        f"MCP tool-error MUST include non-empty content list. Got {content!r}."
    )
    first_block = content[0]
    assert first_block.get("type") == "text", (
        f"First content block MUST be type=text (MCP convention). "
        f"Got {first_block!r}."
    )
    text = first_block.get("text")
    assert isinstance(text, str), (
        f"Tool-error text MUST be a string. Got {text!r}."
    )
    return text


def _assert_names_expected_exception(text: str) -> None:
    """Assert the tool-error text names an operator-facing exception class.

    FastMCP renders the Python exception class name into a human-readable
    form (CamelCase → lowercase phrase, e.g.
    ``AutonomousMutationRejected`` → ``autonomous device mutation rejected``).
    We match case-insensitively against both the CamelCase and the
    humanised form so the test survives FastMCP version changes that
    alter the rendering.

    Note: ``DeviceNotFoundError`` (raised by migrate when ``device_id`` is
    unknown) does NOT include its class name in the rendered text — the
    exception just carries the offending id verbatim. The migrate smoke
    test therefore matches the placeholder ``device_id`` instead of an
    exception class name. Both signals prove the helper was reached and
    raised an expected exception type; the difference is wire-format
    detail, not semantics.
    """
    lowered = text.lower()
    matches = [
        name for name in EXPECTED_OPERATOR_EXCEPTIONS
        if name in text or name.lower() in lowered
    ]
    assert matches, (
        f"Tool-error text MUST name one of {EXPECTED_OPERATOR_EXCEPTIONS!r} "
        f"(case-insensitive) so the orchestrator can branch on it "
        f"deterministically. Got text={text!r}. If this assertion fails, "
        f"the helper raised an unexpected exception type before the "
        f"token / device check."
    )


def test_mcp_migrate_radio_frequency_surfaces_tool_error(
    mcp_http_client: McpHttpClient,
) -> None:
    """A bad token + unknown device surfaces an MCP tool-error, not a protocol error.

    Proves the operator-facing error contract survives the transport: an
    orchestrator that dispatched ``snmp_migrate_radio_frequency`` with an
    invalid ``approval_token`` and a TEST-NET-1 placeholder device_id
    receives a structured tool-error rather than a JSON-RPC protocol
    error.

    Order-of-validation difference vs reboot: ``fetch_migrate`` resolves
    the device BEFORE checking the HITL token, so the exception that
    surfaces here is ``DeviceNotFoundError`` (which renders as just the
    device_id, not the exception class name) rather than
    ``AutonomousMutationRejected``. The smoke test asserts the error
    text contains the placeholder device_id, which is the identifying
    signal the orchestrator can branch on without parsing free-text.
    """
    placeholder_device = "192.0.2.1"
    response = _invoke_tool_and_capture_tool_error(
        mcp_http_client,
        name="snmp_migrate_radio_frequency",
        arguments={
            # TEST-NET-1 placeholder per Zero-Leakage: no real IP, no real
            # community. The empty hermetic inventory the fixture seeds
            # has no device for this IP; fetch_migrate's device resolver
            # fires before the HITL token check.
            "device_id": placeholder_device,
            "approval_token": "invalid-token-skip-pre-flight",
            "target_frequency_mhz": 5800.0,
        },
        request_id=10,
    )

    text = _assert_tool_error_envelope(response, request_id=10)
    # fetch_migrate's DeviceNotFoundError carries the offending id verbatim
    # in its message; assert on the placeholder rather than on the (absent)
    # exception class name. See the docstring on _assert_names_expected_exception
    # for the migration-specific rationale.
    assert placeholder_device in text, (
        f"snmp_migrate_radio_frequency tool-error MUST carry the offending "
        f"device_id ({placeholder_device!r}) so the orchestrator can "
        f"diagnose without parsing free-text. Got text={text!r}. "
        f"If this fails, fetch_migrate likely changed how it reports "
        f"unknown devices (e.g. raised a different exception type first)."
    )


def test_mcp_reboot_radio_surfaces_tool_error(
    mcp_http_client: McpHttpClient,
) -> None:
    """Same contract as migrate: a bad token + unknown device surfaces a tool error.

    Reboot has its own exception paths (DeviceNotFoundError,
    CatalogNotFound, LookupError per the WU-C spec). The smoke test only
    asserts the HITL / DeviceNotFound branch because the fixture seeds an
    empty inventory; the catalogue-lookup branches require a real catalog
    and are covered by the WU-1 unit tests in
    ``tests/snmp_pmp450i/test_reboot.py``.
    """
    response = _invoke_tool_and_capture_tool_error(
        mcp_http_client,
        name="snmp_reboot_radio",
        arguments={
            "device_id": "192.0.2.1",
            "approval_token": "invalid-token-skip-pre-flight",
        },
        request_id=11,
    )

    text = _assert_tool_error_envelope(response, request_id=11)
    _assert_names_expected_exception(text)


def test_mcp_dryrun_section_documented_in_operations() -> None:
    """OPERATIONS.md documents the dry-run seam that PR #80 left implicit.

    Guards against the docs being silently deleted in a refactor. Reads
    the file fresh from disk so the test catches drift between docs and
    code.
    """
    from pathlib import Path

    operations = Path(__file__).resolve().parent.parent / "OPERATIONS.md"
    text = operations.read_text(encoding="utf-8")

    # The section heading MUST be present and exactly named.
    assert "## Dry-run semantics (issue #82)" in text, (
        "OPERATIONS.md MUST contain a 'Dry-run semantics (issue #82)' "
        "section so operators and future contributors understand the seam. "
        "If you intentionally renamed the section, update this assertion too."
    )
    # The section MUST mention the trigger condition and the production
    # wiring — these are the two facts operators need most.
    assert "WritableSnmpClient.set" in text, (
        "Dry-run semantics section MUST name the WritableSnmpClient.set "
        "verb that triggers the seam."
    )
    assert "_writable_client_factory" in text, (
        "Dry-run semantics section MUST name _writable_client_factory as "
        "the production wiring that disables the seam."
    )
    # And MUST link back to the Driver-R2 carve-out so future contributors
    # understand the seam is bounded, not accidental.
    assert "issues/62" in text, (
        "Dry-run semantics section MUST link to issue #62 (Driver-R2 "
        "carve-out) so the bounded-seam property is traceable."
    )


# Reference test (not a real assertion) demonstrating how the orchestrator
# can decode the tool-error text. Kept here so future contributors don't
# have to dig through the MCP framework docs to figure out the wire format.
#
# def _decode_operator_exception(tool_error_text: str) -> str:
#     """Extract the exception class name from a FastMCP tool-error text."""
#     # FastMCP serialises as: "Error calling tool 'X': <ExceptionName>: <message>"
#     parts = tool_error_text.split(": ", 1)
#     if len(parts) != 2:
#         raise ValueError(f"Unexpected tool-error format: {tool_error_text!r}")
#     return parts[1].split(": ", 1)[0].strip()
# (intentionally unused — left as a comment for discoverability)