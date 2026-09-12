"""NORA FastMCP server — thin split.

Boots a FastMCP instance named "nora" over stdio and exposes exactly
four `@mcp.tool` registrations:

* `snmp_get_pmp450i_radio_metrics`         — PMP 450i SNMP driver.
* `search_intervention_history`           — read-only intervention memory.
* `get_device_lifecycle_summary`          — read-only intervention memory.
* `correlate_sector_interference`         — read-only intervention memory.

The thin server contract demands that:

- All logging goes to stderr (stdout is reserved for JSON-RPC frames).
- The tool response never echoes any secret read from `Settings`.
- Free-text error messages are sanitized before they appear in tool output.

The boot sequence is `Settings() -> set_runtime_state(settings) ->
OidCatalogRegistry.verify_all -> Inventory.from_yaml -> set_driver ->
mcp.run()` and lives in `cli.py`; this module only owns the server,
the logging config, the four tools, and a thin log-only middleware that
emits one structured stderr line per tool call.
"""

from __future__ import annotations

import logging
import sys
import time
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware

from nora.config import Settings
from nora.drivers import get_driver
from nora.intervention_memory import tools as intervention_tools
from nora.sanitizer import Sanitizer

logger = logging.getLogger("nora.server")

mcp = FastMCP("nora")

# Module-level state set during boot (`cli.py`) or by tests.
_current_settings: Settings | None = None
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


def set_runtime_state(settings: Settings) -> None:
    """Inject runtime settings (used by `cli` and by tests)."""
    global _current_settings
    _current_settings = settings


def get_runtime_state() -> Settings:
    """Return the boot-time `Settings` instance.

    Raises if the server has not been initialised — this is the production
    safety net against a tool being called before `cli.main()` ran.
    """
    if _current_settings is None:
        raise RuntimeError(
            "NORA server state is not initialised; call nora.server.set_runtime_state() in main()"
        )
    return _current_settings


# ---------------------------------------------------------------------------
# PMP 450i SNMP driver tool
# ---------------------------------------------------------------------------


@mcp.tool
def snmp_get_pmp450i_radio_metrics(device_id: str) -> dict[str, Any]:
    """Fetch a typed `RadioMetricsReport` for the named PMP 450i device.

    The tool body delegates to `Pmp450iDriver.fetch_radio_metrics`,
    which surfaces typed driver exceptions on every wire failure
    (Driver-R5).

    Returns a JSON-serialisable dict (no `dict` / `Any` shape — every
    field is a typed scalar from `RadioMetricsReport.model_dump(mode="json")`).
    """
    driver = get_driver()
    report = driver.fetch_radio_metrics(device_id)
    return report.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Intervention memory MCP tools (read-only).
# ---------------------------------------------------------------------------


@mcp.tool
def search_intervention_history(
    target_ip: str | None = None,
    ticket_number: str | None = None,
    stage: str | None = None,
    keyword: str | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Search the on-disk intervention history written by openchat.

    Filters (AND-combined): `target_ip` (exact), `ticket_number`
    (substring), `stage` (case-insensitive equality), `keyword`
    (substring on serialised record).

    Results are sorted by `timestamp_unix` DESC and capped to `limit`
    (default 5). When `keyword` is provided, at most
    `Settings.nora_interventions_keyword_search_max_records` files are
    read (R7 I/O cap). Every free-text field in every returned record
    is sanitized through `Sanitizer.sanitize(...)`; structured top-level
    fields (`intervention_id`, `target_ip`, `stage`, `status`,
    `timestamp_unix`, `timestamp_iso`, `created_at`, `ticket_number`)
    bypass per the existing `session-journal` R6 contract.

    This tool is read-only — NORA never writes to the interventions
    directory. The hard read-only rule is enforced structurally by an
    AST scan under `tests/intervention_memory/test_no_writes.py`.
    """
    settings = get_runtime_state()
    return intervention_tools.search_intervention_history(
        settings=settings,
        target_ip=target_ip,
        ticket_number=ticket_number,
        stage=stage,
        keyword=keyword,
        limit=limit,
        sanitizer=_sanitizer,
    )


@mcp.tool
def get_device_lifecycle_summary(target_ip: str) -> dict[str, Any]:
    """Return the lifecycle summary for one device.

    Algorithm: `search_intervention_history(target_ip=target_ip, limit=20)`.
    Returns `{"status": "NO_HISTORY_FOUND", "target_ip": ...}` when no
    records match; otherwise a SUCCESS dict with `total_recorded_interventions`,
    `associated_tickets`, `stages_recorded`, `latest_intervention`, and
    `known_pre_existing_offline_subscribers` (extracted from the most
    recent `PRE_DIAGNOSTIC` record).

    All free-text fields sanitized on read; structured fields bypass.
    Read-only — see `search_intervention_history` for the read-only
    guarantee and the AST-guard location.
    """
    settings = get_runtime_state()
    return intervention_tools.get_device_lifecycle_summary(
        settings=settings,
        target_ip=target_ip,
        sanitizer=_sanitizer,
    )


@mcp.tool
def correlate_sector_interference(
    tower_name: str,
    target_frequency_mhz: float,
    channel_width_mhz: float = 20.0,
) -> dict[str, Any]:
    """Detect co-channel and adjacent-channel interference on `tower_name`.

    Walks the most-recent `Settings.nora_interventions_correlate_scan_limit`
    records (default 50) and reports every record whose `system_name`
    contains `tower_name` (substring, case-insensitive) AND whose
    `carrier_frequency_mhz` is within `channel_width_mhz` of
    `target_frequency_mhz`. Each conflict is classified `CO_CHANNEL`
    (delta < 0.5 MHz) or `ADJACENT_CHANNEL`.

    Documented caveat: substring tower match false-positives on short
    prefixes (e.g., `tower_name="A"` matches every `*-A` AP). A
    structured `tower` field is the future fix.

    Read-only — see `search_intervention_history`.
    """
    settings = get_runtime_state()
    return intervention_tools.correlate_sector_interference(
        settings=settings,
        tower_name=tower_name,
        target_frequency_mhz=target_frequency_mhz,
        channel_width_mhz=channel_width_mhz,
        sanitizer=_sanitizer,
    )


# ---------------------------------------------------------------------------
# Thin log-only middleware.
# ---------------------------------------------------------------------------


class _ToolLogMiddleware(Middleware):
    """Emits exactly one structured stderr line per `@mcp.tool` invocation.

    Replaces `_AutoTraceMiddleware` from the pre-thin server. NO journal,
    NO `record_step`; the contract is one `logger.info` line per call
    shaped `tool=<name> duration_ms=<int> outcome=<success|error>`.
    """

    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        start = time.monotonic()
        tool = getattr(getattr(context, "message", None), "name", "<unknown>")
        try:
            result = await call_next(context)
        except Exception:
            duration_ms = int((time.monotonic() - start) * 1000)
            logger.info("tool=%s duration_ms=%d outcome=error", tool, duration_ms)
            raise
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.info("tool=%s duration_ms=%d outcome=success", tool, duration_ms)
        return result


_tool_log_registered: bool = False


def register_tool_log_middleware() -> None:
    """Register the `_ToolLogMiddleware` against the global `mcp` instance.

    Idempotent: a second call is a no-op. Called from `cli.main()` and
    from tests that exercise the middleware.
    """
    global _tool_log_registered
    if _tool_log_registered:
        return
    mcp.add_middleware(_ToolLogMiddleware())
    _tool_log_registered = True


__all__ = [
    "mcp",
    "configure_logging",
    "set_runtime_state",
    "get_runtime_state",
    "snmp_get_pmp450i_radio_metrics",
    "search_intervention_history",
    "get_device_lifecycle_summary",
    "correlate_sector_interference",
    "register_tool_log_middleware",
]
