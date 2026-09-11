"""Auto-trace middleware tests — drives the recording contract end-to-end.

The middleware wraps every `@mcp.tool` invocation in the FastMCP
dispatcher. These tests build a real (in-process) FastMCP server,
register a couple of tools, mount the middleware, and drive them via
the FastMCP `Client`.

Why integration (not pure unit): the middleware's job is to live inside
FastMCP's call chain. Mocking that out would prove the wrapper is
invoked but not that it actually fires on the real tool path.
"""

from __future__ import annotations

import asyncio
import json
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
    """Settings pinned to tmp_path so the journal lives inside the test sandbox."""
    return Settings(
        _env_file=None,
        _env_file_encoding=None,
        nora_session_journal_dir=tmp_path / "sessions",
        nora_session_trace_max_steps=50,
        nora_session_journal_enabled=True,
        nora_operator_alias="test-op",
    )


@pytest.fixture
def journal_dir(hermetic_settings: Settings) -> Path:
    """The journal dir bound to `hermetic_settings`."""
    return hermetic_settings.nora_session_journal_dir


@pytest.fixture
def fresh_journal(hermetic_settings: Settings):
    """A `SessionJournal` bound to `hermetic_settings`."""
    from nora.core.session_journal import SessionJournal
    from nora.sanitizer import Sanitizer

    return SessionJournal.for_settings(hermetic_settings, Sanitizer())


# ---------------------------------------------------------------------------
# R12 — middleware never mutates the tool's response
# ---------------------------------------------------------------------------


def test_invoking_nora_health_records_one_step_and_preserves_4tuple(
    hermetic_settings: Settings,
    fresh_journal,  # noqa: ANN001 — pytest injects
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driving nora_health via a Client produces one journal step; the 4-tuple is byte-identical."""
    import time

    from fastmcp import Client

    # Use a mocked provider so the test does NOT hit the network.
    provider = mock.MagicMock()
    provider.complete.return_value = mock.Mock(text="pong", model_id="m", raw=object())

    # Wire the journal + runtime state into the real server module.
    from nora import server as server_mod

    server_mod.set_runtime_state(hermetic_settings, provider)
    server_mod.init_session_journal(hermetic_settings)
    server_mod.register_auto_trace_middleware()

    try:
        # Drive the actual `nora_health` tool on the real `mcp` instance.
        async def _run() -> tuple[dict[str, Any], list[Any]]:
            async with Client(server_mod.mcp) as client:
                result = await client.call_tool("nora_health", {})
                return (
                    result.data,
                    list(result.content) if result.content is not None else [],
                )

        start = time.monotonic()
        response, content = asyncio.run(_run())
        duration_ms = int((time.monotonic() - start) * 1000)
    finally:
        # Always clean up so other tests start with a fresh middleware chain.
        server_mod.unregister_auto_trace_middleware()

    # The 4-tuple response shape is preserved (R12).
    assert set(response.keys()) == {
        "version",
        "active_provider",
        "connectivity",
        "env_loaded",
    }
    assert response["connectivity"] == "ok"
    assert response["active_provider"] == "lmstudio"

    # Exactly one step recorded for the `nora_health` call.
    test_journal_dir = hermetic_settings.nora_session_journal_dir
    journal_files = list(test_journal_dir.glob("*.json"))
    assert len(journal_files) == 1
    with journal_files[0].open() as f:
        state = json.load(f)
    assert len(state["trace"]) == 1, f"Expected one step; got {state['trace']!r}"
    step = state["trace"][0]
    assert step["tool"] == "nora_health"
    assert step["outcome"] == "success"
    # Sanity: the recorded `duration_ms` is in the same ballpark as our own
    # wall-clock — not zero, not hours.
    assert 0 <= step["duration_ms"] < duration_ms + 1000, (
        f"duration_ms implausible: recorded={step['duration_ms']}, wall={duration_ms}"
    )


# ---------------------------------------------------------------------------
# R12 — tool body that raises records outcome=error and re-raises
# ---------------------------------------------------------------------------


def test_raising_tool_records_outcome_error_and_re_raises(
    fresh_journal,  # noqa: ANN001
) -> None:
    """A tool that raises records `outcome=error` AND re-raises (R12).

    The middleware MUST NOT swallow the exception.
    """
    from fastmcp import Client, FastMCP

    from nora.core.session_journal import SessionJournal
    from nora.sanitizer import Sanitizer
    from nora.server import _AutoTraceMiddleware  # type: ignore[attr-defined]

    # Build a private FastMCP for this test — avoid touching the global mcp.
    mcp = FastMCP("nora-test-error")

    @mcp.tool
    def boom() -> str:
        """Test tool that always raises."""
        raise RuntimeError("simulated tool failure")

    # Mount a fresh middleware against our private journal.
    journal = SessionJournal(
        journal_dir=Path("/tmp") / "nora-test-error-journal",
        max_trace_steps=50,
        sanitizer=Sanitizer(),
        enabled=True,
    )
    try:
        mcp.add_middleware(_AutoTraceMiddleware(journal))

        async def _run() -> Any:
            async with Client(mcp) as client:
                return await client.call_tool("boom", {})

        # The Client surfaces tool errors as McpError; we want to confirm the
        # journal saw outcome=error regardless.
        with pytest.raises(Exception) as excinfo:
            asyncio.run(_run())

        # The error message contains the original cause.
        assert "simulated tool failure" in str(excinfo.value) or "boom" in str(excinfo.value), (
            f"Expected error to mention the simulated failure; got: {excinfo.value!r}"
        )

        # And the journal recorded one step with outcome=error.
        state = journal.get_state()
        assert len(state.trace) == 1
        assert state.trace[0].tool == "boom"
        assert state.trace[0].outcome == "error"
    finally:
        # Clean up the test journal dir.
        import shutil

        jdir = Path("/tmp/nora-test-error-journal")
        if jdir.exists():
            shutil.rmtree(jdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# R15 — NORA_SESSION_JOURNAL_ENABLED disable switch
# ---------------------------------------------------------------------------


@pytest.fixture
def disabled_settings(tmp_path: Path) -> Settings:
    """Settings with the disable switch on (R15)."""
    return Settings(
        _env_file=None,
        _env_file_encoding=None,
        nora_session_journal_dir=tmp_path / "sessions",
        nora_session_trace_max_steps=50,
        nora_session_journal_enabled=False,
        nora_operator_alias="disabled-op",
    )


def test_disable_switch_skips_auto_trace_for_nora_health(
    disabled_settings: Settings,
) -> None:
    """R15-S1 — when disabled, nora_health produces no journal file and 4-tuple is intact."""
    from nora import server as server_mod

    provider = mock.MagicMock()
    provider.complete.return_value = mock.Mock(text="pong", model_id="m", raw=object())
    server_mod.set_runtime_state(disabled_settings, provider)
    server_mod.init_session_journal(disabled_settings)
    server_mod.register_auto_trace_middleware()
    try:

        async def _run() -> dict[str, Any]:
            from fastmcp import Client

            async with Client(server_mod.mcp) as client:
                result = await client.call_tool("nora_health", {})
                return result.data

        response = asyncio.run(_run())

        # The 4-tuple is byte-identical to the enabled case (R12 invariant).
        assert set(response.keys()) == {
            "version",
            "active_provider",
            "connectivity",
            "env_loaded",
        }
        assert response["connectivity"] == "ok"
        assert response["active_provider"] == "lmstudio"

        # No file appears under the journal dir.
        files = list(disabled_settings.nora_session_journal_dir.glob("*.json"))
        assert files == [], f"No file should exist under the journal dir; got {files!r}"
    finally:
        server_mod.unregister_auto_trace_middleware()


def test_disable_switch_makes_explicit_tools_raise(disabled_settings: Settings) -> None:
    """R15-S2 — explicit tools raise JournalDisabledError when disabled."""
    from nora import server as server_mod

    provider = mock.MagicMock()
    provider.complete.return_value = mock.Mock(text="ok", model_id="m", raw=object())
    server_mod.set_runtime_state(disabled_settings, provider)
    server_mod.init_session_journal(disabled_settings)
    server_mod.register_auto_trace_middleware()
    try:

        async def _expect(name: str, args: dict[str, Any]) -> None:
            from fastmcp import Client

            async with Client(server_mod.mcp) as client:
                with pytest.raises(Exception) as exc:
                    await client.call_tool(name, args)
                msg = str(exc.value).lower()
                assert "journaldisabled" in msg.replace(" ", "") or "disabled" in msg, (
                    f"Expected JournalDisabledError from {name!r}; got: {exc.value!r}"
                )

        asyncio.run(_expect("nora_session_get_state", {}))
        asyncio.run(_expect("nora_session_set_focus", {"device_id": "ap-7400-01"}))
        asyncio.run(_expect("nora_session_resume", {"session_id": "any-id"}))
        asyncio.run(_expect("nora_session_summarize", {}))
    finally:
        server_mod.unregister_auto_trace_middleware()
