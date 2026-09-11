"""NORA FastMCP server.

Boots a FastMCP instance named "nora" over stdio and exposes a single tool,
`nora_health`, that surfaces server metadata to LLM agents. The server
contract demands that:

- All logging goes to stderr (stdout is reserved for JSON-RPC frames).
- The tool response never echoes any secret read from `Settings`.
- Free-text error messages are sanitized before they appear in tool output.

The boot sequence is `Settings() -> build_provider() -> mcp.run()` and lives
in `__main__.py`; this module only owns the server, the logging config,
and the tool implementation.
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import datetime, timezone
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware

from nora import __version__
from nora.config import Settings
from nora.core.session_journal import (
    SessionJournal,
    SessionJournalError,
    get_journal,
    init_session_journal,
)
from nora.llm import LLMProvider
from nora.sanitizer import Sanitizer

logger = logging.getLogger("nora.server")

mcp = FastMCP("nora")

# Module-level state set during boot (`__main__.py`) or by tests.
_current_settings: Settings | None = None
_current_provider: LLMProvider | None = None
_sanitizer = Sanitizer()


def configure_logging() -> None:
    """Attach a `StreamHandler(sys.stderr)` to the root logger.

    Idempotent: a second call is a no-op so test suites can call this
    freely without stacking handlers.
    """
    root = logging.getLogger()
    for handler in root.handlers:
        if (
            isinstance(handler, logging.StreamHandler)
            and getattr(handler, "stream", None) is sys.stderr
        ):
            # Already configured.
            root.setLevel(logging.INFO)
            return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def set_runtime_state(settings: Settings, provider: LLMProvider) -> None:
    """Inject runtime dependencies (used by `__main__` and by tests)."""
    global _current_settings, _current_provider
    _current_settings = settings
    _current_provider = provider


def get_runtime_state() -> tuple[Settings, LLMProvider]:
    """Return the boot-time `(settings, provider)` pair.

    Raises if the server has not been initialised — this is the production
    safety net against `nora_health` being called before `__main__` ran.
    """
    if _current_settings is None or _current_provider is None:
        raise RuntimeError(
            "NORA server state is not initialised; call nora.server.set_runtime_state() in main()"
        )
    return _current_settings, _current_provider


# ---------------------------------------------------------------------------
# `nora_health` tool
# ---------------------------------------------------------------------------


# System prompt used for the connectivity probe. Kept short and stable.
_HEALTH_PROBE_SYSTEM = "You are a connectivity probe. Respond with a single short word."


def nora_health_impl(settings: Settings, provider: LLMProvider) -> dict[str, Any]:
    """Compute the `nora_health` response from `settings` and `provider`.

    The public MCP tool (`nora_health`) wraps this; tests inject `settings`
    and `provider` directly so we never need a subprocess for unit tests.
    """
    start = time.monotonic()
    env_loaded = settings.loaded_from == ".env"
    connectivity: str = "ok"
    try:
        provider.complete("ping", system=_HEALTH_PROBE_SYSTEM)
    except Exception as exc:  # noqa: BLE001 - we surface as `unavailable`
        sanitized = _sanitizer.sanitize(str(exc)).text
        logger.warning(
            "nora_health provider call failed: %s",
            sanitized,
        )
        connectivity = "unavailable"

    duration_ms = int((time.monotonic() - start) * 1000)
    logger.info(
        "tool=nora_health duration_ms=%d outcome=%s",
        duration_ms,
        connectivity,
    )

    return {
        "version": __version__,
        "active_provider": settings.nora_llm_provider,
        "connectivity": connectivity,
        "env_loaded": env_loaded,
    }


@mcp.tool
def nora_health() -> dict[str, Any]:
    """Return NORA's version, active provider, connectivity, and .env status.

    The tool is read-only: it calls `provider.complete("ping")` with a fixed
    system prompt and reports success or failure as `connectivity`.
    """
    settings, provider = get_runtime_state()
    return nora_health_impl(settings, provider)


# ---------------------------------------------------------------------------
# SessionJournal explicit recall tools (R7, R9-S2, R14, R16).
#
# Three `@mcp.tool`s registered on the global `mcp` instance. They share
# the module-level `_journal` singleton (set up by `init_session_journal`).
# ---------------------------------------------------------------------------


def _state_to_payload(state: Any) -> dict[str, Any]:
    """Serialise `SessionState` (or compatible model) to a JSON-safe dict."""
    import json

    payload: dict[str, Any] = json.loads(json.dumps(state.model_dump(mode="json")))
    return payload


@mcp.tool
def nora_session_get_state(include_rotated: bool = False) -> dict[str, Any]:
    """Return the current `SessionState` as a JSON-safe dict.

    On a missing canonical file, this call CREATES one with the empty
    default state (R7-S2). When `include_rotated` is True, NDJSON steps
    are merged into `trace` in `step` order (R16-S1).

    Idempotent: consecutive calls return equivalent state (R7-S1).
    """
    journal = get_journal()
    state = journal.get_state(include_rotated=include_rotated)
    return _state_to_payload(state)


@mcp.tool
def nora_session_set_focus(device_id: str) -> dict[str, Any]:
    """Record `device_id` as the active focus and append to `devices_reviewed`.

    Idempotent on the same device id (R7-S4). Advances `last_updated`
    (R14). The state is persisted atomically.
    """
    journal = get_journal()
    state = journal.set_focus(device_id)
    return _state_to_payload(state)


@mcp.tool
def nora_session_resume(session_id: str) -> dict[str, Any]:
    """Load the named session as the active session.

    R7-S5 / R9-S2 — the previously-active session is NOT mutated. The
    in-memory `_session_id` is replaced; subsequent tool calls land on
    the resumed session's canonical file.

    R7-S6 — raises `SessionNotFoundError` if the named file is missing.
    """
    journal = get_journal()
    state = journal.resume(session_id)
    return _state_to_payload(state)


@mcp.tool
def nora_session_summarize() -> str:
    """Return a non-empty Markdown digest of the current session.

    Covers `focus_device_id`, `devices_reviewed`, and the last 10 step
    summaries (R17). Pure projection — does NOT mutate the canonical
    file or record a step.
    """
    journal = get_journal()
    return journal.summarize()


# ---------------------------------------------------------------------------
# Auto-trace middleware (Phase 2 — SessionJournal)
#
# Wraps every `@mcp.tool` invocation in the FastMCP dispatcher. Records
# exactly one `SessionStep` per call, AFTER the tool body returns (or
# raises) — the recording is durability-backed so a crash between the
# tool body and the middleware returning leaves either old or new content
# on disk, never a torn mix (R3).
#
# The middleware is registered exactly once via `register_auto_trace_middleware`,
# typically called from `__main__.py` after `init_session_journal(...)`.
# Tests that need the middleware call it explicitly.
# ---------------------------------------------------------------------------


class _AutoTraceMiddleware(Middleware):
    """Records every `@mcp.tool` call into the module-level `_journal` singleton.

    R3 — `record_step` (atomic JSON write) returns BEFORE this middleware
    returns, so the on-disk state is one step ahead of the in-flight tool.

    R12 — the middleware never mutates `result`. On a tool body exception
    the middleware records with `outcome="error"` then RE-RAISES so the
    JSON-RPC error surface is preserved.
    """

    def __init__(self, journal: SessionJournal) -> None:
        self._journal = journal

    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        # Lazy import keeps `server.py` importable in environments without
        # the auto-trace test setup (e.g., tests that don't exercise it).
        ts = datetime.now(timezone.utc)
        start = time.monotonic()
        message = context.message
        tool = getattr(message, "name", "<unknown>")
        args = getattr(message, "arguments", None) or {}

        try:
            result = await call_next(context)
        except Exception as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            sanitized = self._journal._sanitizer.sanitize(str(exc)).text
            try:
                self._journal.record_step(
                    tool=tool,
                    input_args=args,
                    result_summary=sanitized,
                    duration_ms=duration_ms,
                    outcome="error",
                    ts=ts,
                )
            except SessionJournalError as log_exc:  # pragma: no cover - defensive
                logger.warning("auto-trace failed to record error step: %s", log_exc)
            raise

        duration_ms = int((time.monotonic() - start) * 1000)
        summary = _derive_summary(result)
        try:
            self._journal.record_step(
                tool=tool,
                input_args=args,
                result_summary=summary,
                duration_ms=duration_ms,
                outcome="success",
                ts=ts,
            )
        except SessionJournalError as log_exc:  # pragma: no cover - defensive
            logger.warning("auto-trace failed to record success step: %s", log_exc)
        return result


def _derive_summary(result: Any) -> str:
    """Return a short string summary for `result`.

    Strings pass through (truncated to 256 chars). Other values are
    `repr()`d. The summary is later sanitized on write, so private IPv4 /
    MAC / hostname literals in the string get masked before persistence.
    """
    if isinstance(result, str):
        return result[:256]
    return repr(result)[:256]


_auto_trace_registered: bool = False


def register_auto_trace_middleware(journal: SessionJournal | None = None) -> None:
    """Register the `_AutoTraceMiddleware` against the global `mcp` instance.

    Idempotent: a second call is a no-op (the same middleware stays
    registered for the lifetime of the process). Pass `journal` to use a
    non-default journal; otherwise `get_journal()` is consulted lazily
    on every tool call (so swapping the module-level singleton also swaps
    the journal the middleware writes to).
    """
    global _auto_trace_registered
    if _auto_trace_registered:
        return
    middleware = _AutoTraceMiddleware(journal or get_journal())
    mcp.add_middleware(middleware)
    _auto_trace_registered = True
    logger.info("auto-trace middleware registered")


def unregister_auto_trace_middleware() -> None:
    """Test-only hook — remove the auto-trace middleware from the global mcp.

    Resets the `_auto_trace_registered` flag so a subsequent call to
    `register_auto_trace_middleware()` re-adds a fresh instance.
    """
    global _auto_trace_registered
    _auto_trace_registered = False
    # Best-effort: drop any `_AutoTraceMiddleware` from the chain.
    if hasattr(mcp, "middleware"):
        mcp.middleware = [mw for mw in mcp.middleware if not isinstance(mw, _AutoTraceMiddleware)]


__all__ = [
    "mcp",
    "configure_logging",
    "set_runtime_state",
    "get_runtime_state",
    "nora_health_impl",
    "_AutoTraceMiddleware",
    "register_auto_trace_middleware",
    "unregister_auto_trace_middleware",
    "init_session_journal",  # re-exported for callers that need to wire up
]
