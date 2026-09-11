"""Three explicit recall tools — R7, R9-S2, R14, R16.

These tests drive the tool surface through a real FastMCP `Client`, so
the contract they pin is "what an LLM/operator sees when calling the
tool", not the in-memory Python API.

Tools:
    nora_session_get_state(include_rotated: bool = False) -> dict
    nora_session_set_focus(device_id: str) -> dict
    nora_session_resume(session_id: str) -> dict
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from nora.config import Settings

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def hermetic_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        _env_file_encoding=None,
        nora_session_journal_dir=tmp_path / "sessions",
        nora_session_trace_max_steps=3,  # small so R16 can be exercised cheaply
        nora_session_journal_enabled=True,
        nora_operator_alias="recall-op",
    )


def _wire_server(hermetic_settings: Settings):
    """Set runtime state + journal + middleware. Returns the server module."""
    from nora import server as server_mod

    provider = mock.MagicMock()
    provider.complete.return_value = mock.Mock(text="ok", model_id="m", raw=object())
    server_mod.set_runtime_state(hermetic_settings, provider)
    server_mod.init_session_journal(hermetic_settings)
    server_mod.register_auto_trace_middleware()
    return server_mod


def _cleanup_server() -> None:
    """Best-effort: drop the auto-trace middleware so the next test starts clean."""
    from nora import server as server_mod

    server_mod.unregister_auto_trace_middleware()


async def _call_tool(server_mod, name: str, args: dict[str, Any]) -> Any:
    """Invoke `name` on the real `mcp` instance via a fastmcp Client."""
    from fastmcp import Client

    async with Client(server_mod.mcp) as client:
        return await client.call_tool(name, args)


# ---------------------------------------------------------------------------
# R7-S1 — get_state is idempotent and non-mutating
# ---------------------------------------------------------------------------


def test_get_state_idempotent_and_does_not_mutate(hermetic_settings: Settings) -> None:
    """Two consecutive `nora_session_get_state` calls return equivalent state.

    The tool itself is idempotent — it doesn't mutate `focus_device_id`,
    `devices_reviewed`, or `session_id`. The on-disk `trace` MAY grow
    between calls because the auto-trace middleware records each call,
    but that's an observation, not a state mutation by the tool.
    """
    server_mod = _wire_server(hermetic_settings)
    try:
        result1 = asyncio.run(_call_tool(server_mod, "nora_session_get_state", {}))
        result2 = asyncio.run(_call_tool(server_mod, "nora_session_get_state", {}))

        # Invariants the tool itself enforces (not auto-trace side effects):
        assert result1.data["session_id"] == result2.data["session_id"]
        assert result1.data["focus_device_id"] == result2.data["focus_device_id"] is None
        assert result1.data["devices_reviewed"] == result2.data["devices_reviewed"] == []
        assert result1.data["operator_alias"] == result2.data["operator_alias"]

        # Exactly one canonical file (no duplicates from the two reads).
        files = list(hermetic_settings.nora_session_journal_dir.glob("*.json"))
        assert len(files) == 1, f"Expected one canonical file; got {files!r}"
    finally:
        _cleanup_server()


# ---------------------------------------------------------------------------
# R7-S2 — get_state on missing file creates one (covered in core test)
# This integration test pins the on-the-wire payload shape.
# ---------------------------------------------------------------------------


def test_get_state_creates_canonical_file_on_missing(hermetic_settings: Settings) -> None:
    """First `nora_session_get_state` creates the canonical file with empty trace."""
    server_mod = _wire_server(hermetic_settings)
    try:
        result = asyncio.run(_call_tool(server_mod, "nora_session_get_state", {}))
        assert result.data["trace"] == []
        assert result.data["focus_device_id"] is None
        assert result.data["devices_reviewed"] == []

        files = list(hermetic_settings.nora_session_journal_dir.glob("*.json"))
        assert len(files) == 1
    finally:
        _cleanup_server()


# ---------------------------------------------------------------------------
# R7-S3 — set_focus records and appends to devices_reviewed (R14)
# ---------------------------------------------------------------------------


def test_set_focus_records_device_and_advances_last_updated(
    hermetic_settings: Settings,
) -> None:
    """set_focus persists focus, appends to devices_reviewed, advances last_updated."""
    server_mod = _wire_server(hermetic_settings)
    try:
        # Read once to create the canonical file and capture last_updated (T0).
        first = asyncio.run(_call_tool(server_mod, "nora_session_get_state", {}))
        t0 = first.data["last_updated"]

        result = asyncio.run(
            _call_tool(server_mod, "nora_session_set_focus", {"device_id": "ap-7400-01"})
        )
        assert result.data["focus_device_id"] == "ap-7400-01"
        assert result.data["devices_reviewed"] == ["ap-7400-01"]
        # last_updated advanced (R14).
        assert result.data["last_updated"] > t0, (
            f"last_updated not advanced: {result.data['last_updated']!r} <= {t0!r}"
        )
    finally:
        _cleanup_server()


# ---------------------------------------------------------------------------
# R7-S4 — set_focus idempotent on the same device
# ---------------------------------------------------------------------------


def test_set_focus_idempotent_on_same_device(hermetic_settings: Settings) -> None:
    """set_focus("ap-7400-01") twice → devices_reviewed == ["ap-7400-01"] (length 1)."""
    server_mod = _wire_server(hermetic_settings)
    try:
        asyncio.run(_call_tool(server_mod, "nora_session_set_focus", {"device_id": "ap-7400-01"}))
        result = asyncio.run(
            _call_tool(server_mod, "nora_session_set_focus", {"device_id": "ap-7400-01"})
        )
        assert result.data["devices_reviewed"] == ["ap-7400-01"], (
            f"Second set_focus appended duplicate: {result.data['devices_reviewed']!r}"
        )
        assert len(result.data["devices_reviewed"]) == 1
    finally:
        _cleanup_server()


# ---------------------------------------------------------------------------
# R7-S5 — resume loads an existing session
# ---------------------------------------------------------------------------


def test_resume_loads_existing_session(hermetic_settings: Settings) -> None:
    """resume(session_id) loads a previously-saved session; subsequent writes land there."""
    server_mod = _wire_server(hermetic_settings)
    try:
        # Create a session by calling set_focus.
        first = asyncio.run(
            _call_tool(server_mod, "nora_session_set_focus", {"device_id": "ap-7400-01"})
        )
        sid = first.data["session_id"]

        # Resume the same session explicitly.
        result = asyncio.run(_call_tool(server_mod, "nora_session_resume", {"session_id": sid}))
        assert result.data["session_id"] == sid
        assert result.data["focus_device_id"] == "ap-7400-01"
    finally:
        _cleanup_server()


# ---------------------------------------------------------------------------
# R7-S6 — resume on missing file raises SessionNotFoundError
# ---------------------------------------------------------------------------


def test_resume_missing_file_raises_session_not_found(hermetic_settings: Settings) -> None:
    """resume(bogus-session-id) raises SessionNotFoundError; active session is untouched."""
    server_mod = _wire_server(hermetic_settings)
    try:
        # Create the active session first.
        first = asyncio.run(
            _call_tool(server_mod, "nora_session_set_focus", {"device_id": "ap-7400-01"})
        )
        sid = first.data["session_id"]

        # Resume a bogus session id; expect SessionNotFoundError to surface
        # as an MCP tool error.
        with pytest.raises(Exception) as excinfo:
            asyncio.run(
                _call_tool(server_mod, "nora_session_resume", {"session_id": "bogus-id-xxx"})
            )

        msg = str(excinfo.value).lower()
        assert (
            "sessionnotfounderror" in msg
            or "not found" in msg
            or "sessionnotfound" in msg.replace(" ", "")
        ), f"Expected SessionNotFoundError; got: {excinfo.value!r}"

        # The active session is untouched — get_state still returns the
        # original focus.
        after = asyncio.run(_call_tool(server_mod, "nora_session_get_state", {}))
        assert after.data["session_id"] == sid
        assert after.data["focus_device_id"] == "ap-7400-01"
    finally:
        _cleanup_server()


# ---------------------------------------------------------------------------
# R17 — nora_session_summarize returns non-empty Markdown
# ---------------------------------------------------------------------------


def test_summarize_returns_markdown_with_required_fields(hermetic_settings: Settings) -> None:
    """`nora_session_summarize()` returns Markdown containing focus, devices, and tool names."""
    server_mod = _wire_server(hermetic_settings)
    try:
        # Build a session with a focus, two devices, and at least one tool call.
        asyncio.run(_call_tool(server_mod, "nora_session_set_focus", {"device_id": "ap-7400-01"}))
        asyncio.run(_call_tool(server_mod, "nora_session_set_focus", {"device_id": "ap-7400-02"}))
        # A get_state call will be auto-traced.
        asyncio.run(_call_tool(server_mod, "nora_session_get_state", {}))

        # The summarize tool returns the Markdown as the result data (a string).
        result = asyncio.run(_call_tool(server_mod, "nora_session_summarize", {}))
        markdown = result.data
        assert isinstance(markdown, str), (
            f"summarize must return a string; got {type(markdown).__name__}"
        )
        assert markdown.strip(), "summarize returned empty Markdown"
        # Required fields per R17:
        assert "ap-7400-01" in markdown, f"focus_device_id missing from summary: {markdown!r}"
        assert "ap-7400-02" in markdown, f"second device_id missing from summary: {markdown!r}"
        # At least one tool name from the trace (auto-traced set_focus / get_state).
        assert "nora_session_set_focus" in markdown or "nora_session_get_state" in markdown, (
            f"No tool name from trace in summary: {markdown!r}"
        )
    finally:
        _cleanup_server()


def test_get_state_include_rotated_merges_ndjson_in_step_order(
    hermetic_settings: Settings,
) -> None:
    """When rotation has displaced steps, `include_rotated=True` includes them in order."""
    server_mod = _wire_server(hermetic_settings)
    try:
        # Threshold is 3 (hermetic_settings.nora_session_trace_max_steps).
        # Drive 5 tool calls to force rotation of 2 steps.
        for i in range(5):
            asyncio.run(
                _call_tool(server_mod, "nora_session_set_focus", {"device_id": f"ap-{i:04d}"})
            )

        result_default = asyncio.run(
            _call_tool(server_mod, "nora_session_get_state", {"include_rotated": False})
        )
        result_full = asyncio.run(
            _call_tool(server_mod, "nora_session_get_state", {"include_rotated": True})
        )

        # Default includes only the canonical trace (length up to max_steps).
        assert len(result_default.data["trace"]) <= 3

        # include_rotated=True has more steps than the default.
        full_trace = result_full.data["trace"]
        default_trace = result_default.data["trace"]
        assert len(full_trace) > len(default_trace), (
            f"Expected rotated trace to be longer; got full={len(full_trace)}, "
            f"default={len(default_trace)}"
        )

        # And the full trace is in monotonic step order (displaced steps
        # come first, then canonical).
        steps = [s["step"] for s in full_trace]
        assert steps == sorted(steps), f"Rotated trace out of order: {steps!r}"
    finally:
        _cleanup_server()
