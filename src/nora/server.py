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
from typing import Any

from fastmcp import FastMCP

from nora import __version__
from nora.config import Settings
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


__all__ = [
    "mcp",
    "configure_logging",
    "set_runtime_state",
    "get_runtime_state",
    "nora_health_impl",
]
