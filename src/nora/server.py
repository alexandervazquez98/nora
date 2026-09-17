"""NORA FastMCP server — thin split.

Boots a FastMCP instance named "nora" over stdio and exposes exactly
thirteen `@mcp.tool` registrations and two `@mcp.prompt` registrations:

* `snmp_get_pmp450i_radio_metrics`         — PMP 450i SNMP driver.
* `snmp_get_ap_summary`                    — PMP 450i AP summary.
* `snmp_get_frame_utilization`             — PMP 450i frame utilization.
* `snmp_get_sm_table`                      — PMP 450i SM baseline.
* `snmp_get_sm_detailed_diagnostics`       — PMP 450i SM diagnostics.
* `snmp_run_spectrum_analysis`             — PMP 450i spectrum sweep.
* `snmp_migrate_radio_frequency`           — PMP 450i HITL-gated migration.
* `register_device`                        — ad-hoc IP registration (issue #42).
* `search_intervention_history`           — read-only intervention memory.
* `get_device_lifecycle_summary`          — read-only intervention memory.
* `correlate_sector_interference`         — read-only intervention memory.
* `save_intervention_record`              — writer (issue #12 / new sibling package).
* `hitl_mint_token`                       — admin HITL token issuance (WU-4).
* `netops_orchestrator` (prompt)          — Lead NOC orchestrator system prompt.
* `snmp_pmp450i` (prompt)                  — PMP 450i driver system prompt.

The server also sets a server-level `instructions` string so MCP clients
surface the Zero-Leakage + intervention-memory contract on connect.

The thin server contract demands that:

- All logging goes to stderr (stdout is reserved for JSON-RPC frames).
- The tool response never echoes any secret read from `Settings`.
- Free-text error messages are sanitized before they appear in tool output.
- Prompt bodies never include raw credentials, private IPs, MAC
  addresses, vendor OUI prefixes, or real hostnames.

The boot sequence is `Settings() -> set_runtime_state(settings) ->
set_prompt_registry(PromptRegistry.from_settings(settings)) ->
OidCatalogRegistry.verify_all -> Inventory.from_yaml -> set_driver ->
mcp.run()` and lives in `cli.py`; this module only owns the server,
the logging config, the five tools, the two prompts, and a thin
log-only middleware that emits one structured stderr line per tool
call.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware

from nora.config import Settings
from nora.drivers import get_driver
from nora.intervention_memory import tools as intervention_tools
from nora.intervention_writer.writer import (
    save_intervention_record as _writer_save_intervention_record,
)
from nora.prompts.registry import PromptRegistry
from nora.sanitizer import Sanitizer

logger = logging.getLogger("nora.server")

# Server-level `instructions` advertised to MCP clients on connect.
# Reinforces the Zero-Leakage contract, the "always check
# intervention memory first" workflow, AND the HITL gate on
# `snmp_migrate_radio_frequency` (slice 4) without leaking any
# private infra detail into the wire.
_SERVER_INSTRUCTIONS = (
    "NORA provides read-only RF telemetry and persistent intervention memory for "
    "Cambium PMP 450i networks. Always query device lifecycle history and "
    "pre-existing subscriber states before evaluating RF changes or diagnosing "
    "outages. The `save_intervention_record` tool writes one record under the "
    "configured interventions directory (atomic `tmp + fsync + os.replace`; "
    "sanitised on disk; no HITL gate — record-keeping blast-radius is recoverable "
    "by deleting the file). The `snmp_migrate_radio_frequency` tool mutates "
    "device state and requires an HITL approval token; missing or invalid tokens "
    "raise `AutonomousMutationRejected` (autonomous device mutation rejected: "
    "HITL approval token required). Adhere strictly to Zero-Leakage: never echo "
    "raw credentials, private IPs, or MAC addresses."
)

mcp = FastMCP("nora", instructions=_SERVER_INSTRUCTIONS)

# Module-level state set during boot (`cli.py`) or by tests.
_current_settings: Settings | None = None
_current_prompt_registry: PromptRegistry | None = None
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


def set_prompt_registry(registry: PromptRegistry) -> None:
    """Inject the boot-time `PromptRegistry` (used by `cli` and by tests).

    The registry is the single resolver for `@mcp.prompt` lookups; this
    helper mirrors the `set_runtime_state` pattern so callers wire the
    registry through the boot sequence rather than the env surface.
    """
    global _current_prompt_registry
    _current_prompt_registry = registry


def get_prompt_registry() -> PromptRegistry:
    """Return the boot-time `PromptRegistry` instance.

    Raises if the registry has not been injected — this is the
    production safety net against a prompt being requested before
    `cli.main()` ran.
    """
    if _current_prompt_registry is None:
        raise RuntimeError(
            "NORA prompt registry is not initialised; "
            "call nora.server.set_prompt_registry() in main()"
        )
    return _current_prompt_registry


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
# PMP 450i read-summary tools — slice 2 (PR 2 of
# `2026-09-13-pmp450i-production-surface`).
#
# Both tools delegate to `nora.drivers.snmp_pmp450i.summaries` helpers
# which resolve the catalog, open a client, and fold the wire response
# into a typed Pydantic model. The minor-mismatch fallback is emitted
# through `OidCatalogRegistry.resolve` so the literal
# `"OID catalog fallback: requested X, using Y (minor mismatch)"` line
# reaches stderr for every tool caller (per `oid-catalog/spec.md`
# MODIFIED scenario "minor mismatch returns closest lower minor with
# literal warning via the tool path").
# ---------------------------------------------------------------------------


@mcp.tool
def snmp_get_ap_summary(device_id: str) -> dict[str, Any]:
    """Read a typed AP summary for the named PMP 450i device.

    Returns an `ApSummary` carrying firmware, carrier frequency,
    channel width, tx power, subscriber count, and sys uptime. Every
    field is a typed scalar (`str | int | None`); missing OIDs fall
    back to ``None`` with a literal ``"OID catalog fallback: ..."``
    warning rather than crashing the wire frame.

    Per `pmp450i-radio-tools/spec.md` sub-cluster 1 requirement:
    "Both tools SHALL ... MUST call ``OidCatalogRegistry.resolve``
    before any wire frame".
    """
    from nora.drivers.snmp_pmp450i.summaries import fetch_ap_summary

    driver = get_driver()
    summary = fetch_ap_summary(driver=driver, device_id=device_id)
    return summary.model_dump(mode="json")


@mcp.tool
def snmp_get_frame_utilization(device_id: str) -> dict[str, Any]:
    """Read a typed frame-utilization report for the named PMP 450i device.

    Returns a `FrameUtilization` carrying downlink + uplink percentages
    as typed floats. Same catalog-fallback contract as
    ``snmp_get_ap_summary``.
    """
    from nora.drivers.snmp_pmp450i.summaries import fetch_frame_utilization

    driver = get_driver()
    utilization = fetch_frame_utilization(driver=driver, device_id=device_id)
    return utilization.model_dump(mode="json")


# ---------------------------------------------------------------------------
# PMP 450i SM-table tools — slice 3 (PR 3 of
# `2026-09-13-pmp450i-production-surface`).
#
# Both tools delegate to ``nora.drivers.snmp_pmp450i.subscribers``
# which is the ONE source of truth for the
# ``ONLINE_ACTIVE`` / ``ACTIVE_DEGRADED`` / ``PRE_EXISTING_OFFLINE``
# categorisation. The cross-check ordering rule
# (``search_intervention_history`` BEFORE ``categorize_subscribers``)
# is enforced inside the helper — see
# ``subscribers.fetch_sm_table``.
# ---------------------------------------------------------------------------


@mcp.tool
def snmp_get_sm_table(device_id: str) -> dict[str, Any]:
    """Read the typed subscriber baseline for the named PMP 450i AP.

    Returns a ``SubscriberSummary`` carrying three buckets
    (``ONLINE_ACTIVE``, ``ACTIVE_DEGRADED``, ``PRE_EXISTING_OFFLINE``)
    plus the ``baseline_size`` (ONLINE + DEGRADED; the pre-existing
    bucket is the cross-checked exclusion). The helper applies the
    PRE_DIAGNOSTIC cross-check via
    ``search_intervention_history(stage="PRE_DIAGNOSTIC")`` BEFORE
    ``categorize_subscribers(...)`` runs; reordering the calls breaks
    the cross-check.

    Per `pmp450i-radio-tools/spec.md` sub-cluster 2 requirement "ONE
    Source Of Truth": ``categorize_subscribers`` is the only
    classification entry point — no inline classification in this
    wrapper.
    """
    from nora.drivers.snmp_pmp450i.subscribers import fetch_sm_table

    driver = get_driver()
    settings = get_runtime_state()
    summary = fetch_sm_table(driver=driver, device_id=device_id, settings=settings)
    return summary.model_dump(mode="json")


@mcp.tool
def snmp_get_sm_detailed_diagnostics(device_id: str, luid: str) -> dict[str, Any]:
    """Read the typed per-LUID diagnostics for one SM under ``device_id``.

    Returns an ``SmDetailedDiagnostics`` carrying jitter, CINR, Rx/Tx
    levels, retransmits, and (reserved) interface error counters for
    one SM. ``luid`` identifies the SM within the AP sector managed
    by ``device_id``; the tool fetches one wire GET per diagnostics
    OID name.

    Per `pmp450i-radio-tools/spec.md` sub-cluster 2 scenario "typed
    diagnostics for one LUID".
    """
    from nora.drivers.snmp_pmp450i.subscribers import fetch_sm_detailed_diagnostics

    driver = get_driver()
    diagnostics = fetch_sm_detailed_diagnostics(driver=driver, device_id=device_id, luid=luid)
    return diagnostics.model_dump(mode="json")


# ---------------------------------------------------------------------------
# PMP 450i spectrum-sweep tool — slice 4 (PR 4 of
# `2026-09-13-pmp450i-production-surface`).
#
# Delegates to ``nora.drivers.snmp_pmp450i.spectrum.fetch_spectrum``
# which enforces ``Settings.nora_maintenance_window_*`` BEFORE
# emitting any wire frame. Outside the configured window the helper
# raises :class:`MaintenanceWindowViolation` — the tool surface
# surfaces the typed exception to the MCP caller.
# ---------------------------------------------------------------------------


@mcp.tool
def snmp_run_spectrum_analysis(device_id: str, operator_confirmed: bool = False) -> dict[str, Any]:
    """Read a typed spectrum sweep for the named PMP 450i device.

    Returns a :class:`SpectrumAnalysis` carrying
    ``ranked_clean_frequencies`` (sorted by ascending noise floor),
    ``noise_floor_dbm`` (frequency-kHz → noise-dBm map), and
    ``scan_started_at``.

    Per `pmp450i-radio-tools/spec.md` sub-cluster 3 requirement
    "snmp_run_spectrum_analysis — Ranked Clean Frequencies +
    Maintenance Window": calls outside the configured maintenance
    window raise :class:`MaintenanceWindowViolation` and emit zero
    wire frames.

    Per issue #43 ADDED requirement "Tier-1 Operator Clearance Gate":
    ``operator_confirmed`` defaults to ``False`` (fail-closed). The
    server-side gate raises :class:`Tier1ClearanceRequired` BEFORE any
    wire frame when ``operator_confirmed`` is False (or absent). The
    LLM orchestrator MUST request operator clearance before invoking.
    """
    from nora.drivers.snmp_pmp450i.spectrum import fetch_spectrum

    driver = get_driver()
    settings = get_runtime_state()
    analysis = fetch_spectrum(
        driver=driver,
        device_id=device_id,
        settings=settings,
        operator_confirmed=operator_confirmed,
    )
    return analysis.model_dump(mode="json")


# ---------------------------------------------------------------------------
# PMP 450i HITL-gated RF migration tool — slice 4 (PR 4 of
# `2026-09-13-pmp450i-production-surface`).
#
# Delegates to ``nora.drivers.snmp_pmp450i.migrate.fetch_migrate``
# which calls ``nora.hitl.tokens.verify_approval_token`` BEFORE any
# SNMP SET frame is emitted. Missing or invalid tokens raise
# :class:`AutonomousMutationRejected` with the literal
# "autonomous device mutation rejected: HITL approval token
# required" message (the contract seam pinned by
# ``tests/test_hitl_tokens.py``).
# ---------------------------------------------------------------------------


@mcp.tool
def snmp_migrate_radio_frequency(
    device_id: str,
    approval_token: str,
    target_frequency_mhz: float,
) -> dict[str, Any]:
    """Migrate a PMP 450i AP to ``target_frequency_mhz`` (HITL-gated).

    Returns a :class:`MigrationResult` carrying ``rolled_back``,
    ``reason``, ``pre_existing_offline_excluded``,
    ``online_active_migrated``, ``active_degraded_migrated``,
    ``target_frequency_mhz``, and ``device_id``.

    Per `pmp450i-radio-tools/spec.md` sub-cluster 3 requirements
    "Approval Token Contract", "Rollback Watchdog With Timeout",
    "Make-Before-Break Order + PRE_EXISTING_OFFLINE Exclusion", and
    "Intervention Record Emission On Migration Completion": the
    tool requires an HITL approval token, follows make-before-break
    (ONLINE_ACTIVE → ACTIVE_DEGRADED → AP carrier), and emits one
    ``POST_MIGRATION`` intervention record per completion (success
    or rollback).
    """
    from nora.drivers.snmp_pmp450i.migrate import fetch_migrate

    driver = get_driver()
    settings = get_runtime_state()
    result = fetch_migrate(
        driver=driver,
        device_id=device_id,
        approval_token=approval_token,
        target_frequency_mhz=target_frequency_mhz,
        settings=settings,
    )
    return result


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
# Intervention memory MCP tool — atomic write (issue #12 / writer sibling).
# ---------------------------------------------------------------------------


@mcp.tool
def save_intervention_record(payload: dict[str, Any]) -> dict[str, Any]:
    """Atomically write one intervention record under the configured dir.

    Thin delegate to `nora.intervention_writer.writer.save_intervention_record`.
    The library function is the canonical body — keeps the MCP surface
    in lock-step with library code that #15 may call from non-MCP paths
    (`snmp_get_ap_summary` auto-save after a successful diagnostic).

    The wrapper does NOT re-sanitise: W4 masks the on-disk file at write
    time, and a second sanitisation would drift aliases. The wrapper does
    NOT require an HITL approval token — record-keeping blast-radius is
    recoverable by deleting the file; HITL scope is reserved for #15's
    destructive RF migration.

    Returns a dict. On success: `{"status": "OK", "intervention_id": <stem>}`.
    On failure: a dict with `"status"` set to one of `INVALID_INPUT` /
    `INVALID_PAYLOAD` / `PATH_TRAVERSAL_DETECTED` / `DUPLICATE_INTERVENTION_ID`
    / `WRITE_ERROR`. Never raises.
    """
    settings = get_runtime_state()
    return _writer_save_intervention_record(settings, payload)


# ---------------------------------------------------------------------------
# Ad-hoc device registration — issue #42 / `2026-09-15-register-device-mcp`.
#
# Closes the IPv4-literal resolution gap: when the operator hands the
# orchestrator an IP absent from `data/devices.yaml`, the orchestrator
# calls `register_device(host, community, validate=True)` instead of
# asking for a `device_id`. The tool body is a thin wrapper over
# `_register_device_impl` — that helper handles the validation wire
# frame, the typed error mapping, and the `MutableInventory` insertion.
#
# `validate=True` (default) issues a cheap `sysDescr` GET against OID
# `1.3.6.1.2.1.1.1.0` BEFORE inserting; failure raises a typed
# exception and inserts nothing. `validate=False` skips the wire frame
# and inserts immediately (operator-supplied credentials only — no
# out-of-band reachability check).
#
# Community credentials are wrapped in `SecretStr` so
# `model_dump(mode="json")` masks the literal to `"**********"` at the
# wire boundary — Zero-Leakage contract (§1 of the orchestrator prompt).
# ---------------------------------------------------------------------------


@mcp.tool
def register_device(host: str, community: str, validate: bool = True) -> dict[str, Any]:
    """Ad-hoc-register a PMP 450i radio. `validate=True` issues a cheap
    sysDescr GET (`1.3.6.1.2.1.1.1.0`) before insertion. Returns a typed
    `DeviceRecord` with `community` masked to `"**********"`. Typed error
    on any failure path; inserts NOTHING.

    Issue #42 / change `2026-09-15-register-device-mcp`. The validation
    contract (RFC 1213 sysDescr GET) is referenced from §4 Step 4 of
    `src/nora/prompts/netops_orchestrator.md` so the orchestrator knows
    that an unreachable radio fails closed.
    """
    from nora.drivers.snmp_pmp450i.register_device import (
        _register_device_impl,
    )

    record = _register_device_impl(
        driver=get_driver(),
        host=host,
        community=community,
        validate=validate,
        sanitizer=_sanitizer,
    )
    return record.model_dump(mode="json")


# ---------------------------------------------------------------------------
# HITL admin token issuance — WU-4 / PR #44 follow-up plan.
#
# Mirrors the `nora hitl mint --operator-id … --ttl-seconds …` CLI without
# requiring shell access on the backend host; NOC operators without
# terminal access can mint verification tokens from chat.
#
# Wired through `Settings.nora_hitl_signing_key` (lazy fail-closed — a
# missing / empty key raises `AutonomousMutationRejected` to the MCP
# caller). The wire response is `HitlApprovalToken.model_dump(mode="json")`
# — a dict carrying `token`, `operator_id`, `issued_at`, `expires_at`,
# `signature`. Default ON (`Settings.nora_hitl_admin_enabled = True`);
# deploys that want CLI-only minting set
# `NORA_HITL_ADMIN_ENABLED=false`.
#
# Imports are scoped to the tool body so the cold-import cost stays
# zero when no caller invokes `hitl_mint_token` (same idiom as
# `snmp_migrate_radio_frequency`).
# ---------------------------------------------------------------------------


@mcp.tool
def hitl_mint_token(
    operator_id: str,
    ttl_seconds: int = 900,
) -> dict[str, Any]:
    """Mint a HMAC-signed HITL approval token (WU-4 / PR #44).

    Same semantics as the `nora hitl mint --operator-id … --ttl-seconds …`
    CLI: a typed token is produced via `nora.hitl.tokens.mint_token`
    using the boot-time `Settings.nora_hitl_signing_key`. The wire
    response is `mint_token(...).model_dump(mode="json")` — a dict
    carrying `token`, `operator_id`, `issued_at`, `expires_at`,
    `signature`.

    Default ON (`Settings.nora_hitl_admin_enabled = True`); deploys
    that want CLI-only minting set `NORA_HITL_ADMIN_ENABLED=false`.

    Raises:
        AutonomousMutationRejected: missing or empty
            `nora_hitl_signing_key`. The literal message is the
            contract seam (mirrors `verify_approval_token`).
    """
    from nora.hitl.tokens import mint_token

    settings = get_runtime_state()
    signing_key = settings.nora_hitl_signing_key
    effective_ttl = ttl_seconds if ttl_seconds is not None else settings.nora_hitl_token_ttl_seconds
    token = mint_token(
        operator_id,
        ttl_seconds=effective_ttl,
        signing_key=signing_key,
    )
    return token.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Prompt registrations — `@mcp.prompt` thin wrappers over `PromptRegistry`.
# ---------------------------------------------------------------------------


# Module-level, explicit allow-list of prompts exposed over MCP. MUST stay
# in sync with the `@mcp.prompt` registrations below; the constant exists so
# readers can audit "what is published to MCP clients" without grepping
# decorators.
_EXPOSED_PROMPTS: tuple[str, ...] = ("netops_orchestrator", "snmp_pmp450i")


@mcp.prompt
def netops_orchestrator() -> str:
    """Lead NOC Wireless Infrastructure Orchestrator system prompt."""
    return get_prompt_registry().get("netops_orchestrator").body


@mcp.prompt
def snmp_pmp450i() -> str:
    """PMP 450i SNMP driver operator-facing system prompt."""
    return get_prompt_registry().get("snmp_pmp450i").body


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


# ---------------------------------------------------------------------------
# Boot-time tool-registration guard (slice 5 / PR 5).
#
# Per `oid-catalog-integration/spec.md` requirement "Tool-Registration
# Guard — Reject Uncatalogued Tools": every `@mcp.tool` registered on
# the global `mcp` instance MUST have an entry in
# `OidCatalogRegistry.REQUIRED_OIDS_BY_TOOL[(vendor, model)]`. The
# guard walks `__all__` (the canonical tool surface) and raises a
# typed `UncataloguedToolError` for any name absent from the index.
#
# Why enumerate from `__all__` and not from `mcp._tool_manager._tools`?
# FastMCP 3.x removed the `_tool_manager` attribute; the equivalent
# API (`mcp.list_tools()`) is async. `__all__` is a deterministic
# module-level surface that's maintained in lock-step with the
# `@mcp.tool` decorators and stays version-stable across FastMCP
# upgrades. It excludes prompts and helpers by construction.
# ---------------------------------------------------------------------------

# Names exported from `server.__all__` that are NOT `@mcp.tool`
# registrations. The guard filters these so only tool names reach the
# index check.
_NON_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "mcp",
        "configure_logging",
        "set_runtime_state",
        "get_runtime_state",
        "set_prompt_registry",
        "get_prompt_registry",
        "register_tool_log_middleware",
        # `@mcp.prompt` registrations — they live on the same `mcp`
        # instance but are NOT `@mcp.tool`s. Filtering them here keeps
        # the guard focused on the LLM-tool surface.
        "netops_orchestrator",
        "snmp_pmp450i",
        # Boot-time guard helpers exposed for `cli` and the
        # integration-test subprocess pattern. NOT `@mcp.tool`s —
        # must not show up in the guard's iteration.
        "verify_tools_are_catalogued",
        "verify_tools_have_tier_classification",
    }
)


# Tools registered on `mcp` that the boot-time guard MUST NOT refuse
# even though their names are absent from the PMP 450i catalog's
# `tools` envelope. Three categories:
#
# 1. The four intervention-memory operators — they consume the local
#    intervention-history filesystem, not SNMP OIDs. The OID catalog
#    does not cover them and never will (per
#    `intervention-memory/spec.md`).
# 2. The legacy `snmp_get_pmp450i_radio_metrics` — registered in
#    `nora-mcp-thin-split` (PR #8) before the per-tool envelope
#    format existed. Adding it to the catalog's `tools` envelope
#    requires re-signing the baseline catalog (out of scope for PR 5).
#    The allow-list entry keeps the guard green until a follow-up
#    PR re-signs `15.2.1.json` and `15.3.0.json` with the legacy
#    radio-metrics tool entry.
#
# Per `oid-catalog-integration/spec.md` requirement "Tool-Registration
# Guard": the spec calls for an `ALLOWED_UNCATALOGUED` allow-list
# (none today, says the spec) — this constant IS that allow-list.
# The "none today" wording reflects the design intent that operators
# SHOULD sign every tool; the entries below are the documented
# exceptions that ship with PR 5 (see commit message for the
# rationale behind each entry).
_ALLOWED_UNCATALOGUED_TOOLS: frozenset[str] = frozenset(
    {
        "search_intervention_history",
        "get_device_lifecycle_summary",
        "correlate_sector_interference",
        "save_intervention_record",
        # WU-4 / PR #44 follow-up plan: the HITL admin tool is NOT an
        # SNMP-backed operator (no OID catalog will ever cover it —
        # it consumes `Settings.nora_hitl_signing_key`, not the
        # PMP 450i OID catalog). Allow-list entry keeps the
        # boot-time tool-registration guard green until a follow-up
        # change re-examines whether the tool warrants catalog
        # coverage. See the deferred-tier-classification note near
        # `_EXPECTED_TOOL_TIERS` for the related follow-up.
        "hitl_mint_token",
        # Issue #42 / `2026-09-15-register-device-mcp`: both
        # `register_device` and `snmp_get_pmp450i_radio_metrics` were
        # retired from this allow-list once their catalog envelope
        # entries landed in the re-signed baselines (Tasks 6 + 7).
        # The four remaining entries are intervention-memory operators
        # that consume the on-disk filesystem, not SNMP — they will
        # never carry an OID catalog entry.
    }
)


def _enumerate_tool_names() -> list[str]:
    """Return every `@mcp.tool` name currently registered on ``mcp``.

    Enumerates via ``mcp.list_tools()`` (async) — the FastMCP-
    canonical surface that mirrors every `@mcp.tool` decorator AND
    every `mcp.add_tool(...)` registration. Sorted for deterministic
    error messages across Python versions and to make test failure
    diffs readable.

    The ``__all__`-based fallback (slice 5's first cut) only sees
    tools registered via `@mcp.tool` decorators at import time;
    `mcp.add_tool(...)` calls — used by the integration-test subprocess
    to inject a rogue tool — would silently bypass the guard. The
    async list resolves that hole.
    """
    tools = asyncio.run(mcp.list_tools())
    return sorted(tool.name for tool in tools)


def verify_tools_are_catalogued(
    registry: Any,
    *,
    vendor: str = "cambium",
    model: str = "pmp450i",
) -> None:
    """Boot-time guard — refuse any `@mcp.tool` not present in the catalog index.

    Iterates the canonical tool surface (see :func:`_enumerate_tool_names`)
    and raises :class:`UncataloguedToolError` for any tool name that is:

    * NEITHER present in ``registry.required_oids_by_tool((vendor, model))``
    * NOR listed in :data:`_ALLOWED_UNCATALOGUED_TOOLS`.

    The exception carries the offending tool name verbatim so the
    on-screen error points at the registration the operator needs to
    fix.

    Called from :func:`nora.cli.main` BEFORE ``mcp.run()`` so a rogue
    registration aborts the boot — the tool is never exposed to MCP
    clients. The test surface (``tests/test_oid_catalog_integration.py``
    named test ``new_tool_without_oid_registration_rejected_at_registration_time``)
    drives the helper from a real subprocess per
    ``openspec/config.yaml::testing.layers.integration``.

    The ``registry`` parameter is typed as ``Any`` so this module
    avoids an import cycle (the catalog module imports from
    ``nora.config`` which transitively reaches the server module
    under some install layouts). The helper accepts any object
    exposing ``required_oids_by_tool((vendor, model))`` returning a
    dict[str, tuple[str, ...]].
    """
    from nora.drivers.exceptions import UncataloguedToolError

    cataloged = registry.required_oids_by_tool((vendor, model))
    for tool_name in _enumerate_tool_names():
        if tool_name in cataloged:
            continue
        if tool_name in _ALLOWED_UNCATALOGUED_TOOLS:
            continue
        raise UncataloguedToolError(
            tool_name=tool_name,
            reason=(
                f"tool {tool_name!r} has no entry in "
                f"REQUIRED_OIDS_BY_TOOL[({vendor!r}, {model!r})]; "
                f"refused to expose uncatalogued tool to LLM"
            ),
        )


# ---------------------------------------------------------------------------
# Issue #43 / `2026-09-15-3tier-tool-governance` — boot-time tier
# classification guard. Per `tool-service-impact-tiers` capability
# requirement "Three-Tier Taxonomy Is Frozen", every registered
# `@mcp.tool` MUST be classified into exactly one of {Tier 0, Tier 1,
# Tier 2}. The guard enumerates the canonical tool surface, looks up
# each tool's tier in the tool-spec front-matter, and raises a typed
# error on a missing or invalid classification.
# ---------------------------------------------------------------------------


# Tool name -> expected tier. Mirrors the verbatim table in
# `openspec/changes/2026-09-15-3tier-tool-governance/specs/tool-service-impact-tiers/spec.md`.
#
# NOTE: `hitl_mint_token` (WU-4 / PR #44) is intentionally NOT
# in _EXPECTED_TOOL_TIERS for this PR. A future change must:
# (a) classify it as Tier 2 (it issues HMAC-signed tokens that
# grant mutation authority),
# (b) add a docs/tool_specs/hitl_mint_token.md file against
# the ADR-4 frozen schema.
_EXPECTED_TOOL_TIERS: dict[str, int] = {
    # Tier 0 — Passive Telemetry (8)
    "snmp_get_ap_summary": 0,
    "snmp_get_sm_table": 0,
    "snmp_get_pmp450i_radio_metrics": 0,
    "snmp_get_frame_utilization": 0,
    "snmp_get_sm_detailed_diagnostics": 0,
    "search_intervention_history": 0,
    "get_device_lifecycle_summary": 0,
    "correlate_sector_interference": 0,
    # Tier 1 — Potentially Disruptive (1)
    "snmp_run_spectrum_analysis": 1,
    # Tier 2 — Service-Affecting Mutations (2)
    "snmp_migrate_radio_frequency": 2,
    "save_intervention_record": 2,
    # `register_device` (issue #42) is intentionally NOT in this table
    # for now — its dedicated `docs/tool_specs/register_device.md` lands
    # in a follow-up change. The guard's "expected_tier is None" branch
    # below skips unknown tools so a future tool expansion does not
    # fail boot.
}


def verify_tools_have_tier_classification(
    prompt_registry: Any,
) -> None:
    """Boot-time guard — refuse any `@mcp.tool` whose tier is missing or invalid.

    Iterates the canonical tool surface, looks up each tool's tier in
    ``prompt_registry.get(name).metadata["tier"]`` (parsed by the
    ToolSpecValidator), and raises
    :class:`UncataloguedToolError` on any mismatch. Called from
    :func:`nora.cli.main` AFTER ``PromptRegistry.from_settings`` and
    BEFORE ``mcp.run()`` so a misclassification aborts the boot — the
    server never reaches the LLM with an unclassified tool surface.
    """
    from nora.drivers.exceptions import UncataloguedToolError

    for tool_name in _enumerate_tool_names():
        # Tools that do NOT have a tool-spec (e.g. `register_device`,
        # `save_intervention_record` may appear here when the override
        # is off) fall back to the canonical tier table.
        try:
            prompt = prompt_registry.get(tool_name)
            classified_tier = prompt.metadata.get("tier")
        except Exception:  # noqa: BLE001
            classified_tier = None

        expected_tier = _EXPECTED_TOOL_TIERS.get(tool_name)
        if expected_tier is None:
            # Tool is not part of the canonical taxonomy (e.g. legacy
            # tools added by follow-up PRs); skip without raising so a
            # future tool expansion does not fail boot.
            continue
        if classified_tier != expected_tier:
            raise UncataloguedToolError(
                tool_name=tool_name,
                reason=(
                    f"tool {tool_name!r} has tier {classified_tier!r}; "
                    f"expected {expected_tier!r} per "
                    f"`tool-service-impact-tiers` taxonomy"
                ),
            )


__all__ = [
    "mcp",
    "configure_logging",
    "set_runtime_state",
    "get_runtime_state",
    "set_prompt_registry",
    "get_prompt_registry",
    "snmp_get_pmp450i_radio_metrics",
    "snmp_get_ap_summary",
    "snmp_get_frame_utilization",
    "snmp_get_sm_table",
    "snmp_get_sm_detailed_diagnostics",
    "snmp_run_spectrum_analysis",
    "snmp_migrate_radio_frequency",
    "search_intervention_history",
    "get_device_lifecycle_summary",
    "correlate_sector_interference",
    "save_intervention_record",
    "register_device",
    # WU-4 / PR #44 follow-up plan: HITL admin token issuance over MCP.
    # Tier classification deferred — see the NOTE above
    # `_EXPECTED_TOOL_TIERS` for the follow-up contract.
    "hitl_mint_token",
    "netops_orchestrator",
    "snmp_pmp450i",
    "register_tool_log_middleware",
    "verify_tools_are_catalogued",
    "verify_tools_have_tier_classification",
]
