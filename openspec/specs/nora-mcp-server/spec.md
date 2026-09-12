# nora-mcp-server Specification

## Purpose

Defines how NORA boots the FastMCP server over stdio, registers the `nora_health` tool as the first MCP-facing surface, and enforces the hard constraint that all logging goes to stderr — never stdout, which is reserved for the JSON-RPC stream. This capability is the Phase 1 deliverable that lets an operator's LLM agent discover NORA's version, active provider, connectivity, and `.env` load status.

## Requirements

### Requirement: FastMCP Boot Over Stdio

The server MUST boot via `FastMCP("nora")` and register exactly four `@mcp.tool` functions over stdio. FastMCP MUST be pinned to `>=3.2,<4`. Boot MUST NOT construct `LLMProvider`, MUST NOT call `init_session_journal`, MUST NOT register `_AutoTraceMiddleware`.

#### Scenario: server registers and runs over stdio

- GIVEN `src/nora/cli.py` imports `mcp` from `src/nora/server.py`
- WHEN `mcp.run()` is called
- THEN the server listens on stdio AND NORA code does not write to stdout

#### Scenario: pinned FastMCP version

- GIVEN `pyproject.toml`
- WHEN its dependencies are listed
- THEN `fastmcp` is pinned to `>=3.2,<4`

### Requirement: Stderr-Only Logging

Every log line emitted by NORA MUST go to stderr. No code path under `src/nora/` MAY write to stdout.

#### Scenario: stderr receives a startup log line

- GIVEN the server starts
- WHEN stderr is captured
- THEN a log line names the boot surface

#### Scenario: stdout is reserved for JSON-RPC

- GIVEN the server is processing a request
- WHEN stdout is captured for 1 second
- THEN the only bytes on stdout are JSON-RPC frames

#### Scenario: a stray `print()` is caught at lint time

- GIVEN `src/nora/server.py` contains `print("debug")`
- WHEN `ruff check .` runs
- THEN the violation is reported and exit code is non-zero

### Requirement: Telemetry Sanitizer Boundary

Every free-text value leaving the process via an MCP tool response MUST pass through the telemetry sanitizer before serialisation. Typed structured fields MAY opt out.

#### Scenario: free-text fields are sanitized

- GIVEN an upstream error message contains a private IPv4 literal
- WHEN `correlate_sector_interference(...)` is invoked
- THEN the literal is replaced by a synthetic alias

#### Scenario: structured fields bypass the sanitizer

- GIVEN `search_intervention_history` returns its typed top-level fields
- WHEN the response is serialised
- THEN `intervention_id`, `target_ip`, `stage`, `status`, `timestamp_unix` are NOT run through the sanitizer

### Requirement: Security Boundary — No Secrets in Tool Responses

Every MCP tool MUST NOT include any secret value from `Settings` in any response field, error message, or log line.

#### Scenario: signing key never appears in tool response

- GIVEN `Settings.nora_oid_catalog_signing_key` is set
- WHEN any of the four tools is invoked
- THEN no response field contains the signing key value

#### Scenario: signing key never appears in error messages

- GIVEN the driver raises an `InventoryError`
- WHEN the tool returns its error message
- THEN the message does NOT contain the signing key value

### Requirement: Observability — Stderr Tool Diagnostics

Each tool invocation MUST emit one structured log line on stderr with tool name, duration, and outcome.

#### Scenario: tool call emits a structured log line

- GIVEN `snmp_get_pmp450i_radio_metrics` is invoked
- WHEN the call completes
- THEN one stderr line is emitted with tool name, duration, and outcome

### Requirement: R-NEW-1 — Four `@mcp.tool` Registrations

The server SHALL register exactly four `@mcp.tool`-decorated functions on the global `mcp = FastMCP("nora")` instance: one driver + three intervention memory. The driver tool SHALL NOT call `nora_session_set_focus`. The four names SHALL be re-exported in `__all__`. No `_AutoTraceMiddleware` is registered.

#### ADDED Scenario: server module exports the four tool names

- GIVEN `src/nora/server.py` imports the driver + three intervention callables
- WHEN the four names are imported from `nora.server`
- THEN the imports succeed AND all four names appear in `nora.server.__all__`

#### ADDED Scenario: MCP wrappers delegate to library functions

- GIVEN `set_runtime_state(settings=fake_settings)` has been called
- WHEN `search_intervention_history(target_ip="10.0.0.5")` MCP tool is invoked
- THEN `nora.intervention_memory.tools.search_intervention_history` is called exactly once AND the wrapper returns the library result unchanged

#### ADDED Scenario: driver tool no longer calls `nora_session_set_focus`

- GIVEN the driver tool is invoked with `device_id="ap-7400-01"`
- WHEN the call body executes
- THEN no `nora_session_set_focus(...)` call is attempted AND no journal file is created as a side-effect

### Requirement: R-NEW-2 — Sanitizer Bound at Tool Boundary

Every free-text field in the four surviving tools' output SHALL pass through `Sanitizer.sanitize(...)` before serialization. Structured top-level fields SHALL bypass.

#### ADDED Scenario: free-text fields in `search_intervention_history` are sanitized

- GIVEN a record whose `record_name` contains the literal `10.53.12.4`
- WHEN `search_intervention_history(target_ip=...)` is invoked via the MCP tool
- THEN the returned list contains a dict whose `record_name` does NOT include `10.53.12.4` AND includes a `RADIO_NODE_*` alias

#### ADDED Scenario: structured top-level fields bypass the sanitizer

- GIVEN a record whose `intervention_id == "INT-1-10.0.0.5-1234567-XYZ"`
- WHEN `search_intervention_history(...)` returns the record via the MCP tool
- THEN `record["intervention_id"]`, `record["timestamp_unix"]`, and `record["stage"]` are all byte-identical to the on-disk values

### Requirement: R-NEW-3 — Hard Read-Only Contract (AST Guard)

The three intervention tools SHALL NOT mutate any file. `tests/intervention_memory/test_no_writes.py` SHALL fail any commit that introduces a writable file operation under `src/nora/intervention_memory/`. The driver tool is out of scope for this guard.

#### ADDED Scenario: the AST read-only guard fails on an injected write

- GIVEN a developer adds `Path("/tmp/x").write_text("x")` inside `src/nora/intervention_memory/storage.py`
- WHEN `uv run pytest tests/intervention_memory/test_no_writes.py` runs
- THEN the test exits non-zero AND the failure message identifies `(storage.py, <line>, "write_text")`

### Requirement: R-NEW-4 — One-Way Cross-Capability Dependency Direction

`src/nora/server.py` MAY import from `nora.intervention_memory.tools` and from `nora.drivers.snmp_pmp450i.driver`. No module under `src/nora/intervention_memory/` or `src/nora/drivers/` SHALL import from `nora.server`. `shim_webui.py` remains a one-way consumer external to NORA.

#### ADDED Scenario: `nora-mcp-server` imports from both consumer packages

- GIVEN `src/nora/server.py` adds the import block from `nora.intervention_memory.tools` and from `nora.drivers.snmp_pmp450i.driver`
- WHEN the server module is imported
- THEN no import error is raised AND the four `@mcp.tool` registrations bind to the imported callables

#### ADDED Scenario: consumer packages do not import from `nora.server`

- GIVEN every `.py` under `src/nora/intervention_memory/` and `src/nora/drivers/` except `shim_webui.py`
- WHEN a static grep scans for `from nora.server` or `import nora.server`
- THEN zero matches are found

### Requirement: `python -m nora` Deprecation Alias

`python -m nora` MUST emit a `DeprecationWarning` whose message contains `will be removed in the next minor release` and MUST delegate to the `nora-mcp` boot sequence, exposing the identical four-tool surface.

#### Scenario: alias emits the deprecation warning and boots the thin surface

- GIVEN the package is installed in editable mode
- WHEN `python -m nora` is invoked as a subprocess
- THEN stderr contains one line matching `DeprecationWarning` and `will be removed in the next minor release` AND the four-tool surface is reachable via JSON-RPC

#### Scenario: alias and `nora-mcp` expose identical tool lists

- GIVEN both entry points are reachable
- WHEN each is asked for its `tools/list` over stdio
- THEN the two responses contain the same four tool names in the same order

### Requirement: No Boot-Time LLM or Journal Injection

The canonical `nora-mcp` boot (`src/nora/cli.py`) MUST wire `Settings() -> PromptRegistry.from_settings -> OidCatalogRegistry.verify_all -> Inventory.from_yaml -> set_driver -> mcp.run(show_banner=False)`. Boot MUST NOT construct `LLMProvider`, call `init_session_journal`, call `build_provider`, or register `_AutoTraceMiddleware`.

#### Scenario: cli.py has no LLM/journal/middleware wiring

- GIVEN `src/nora/cli.py`
- WHEN its source is scanned for `build_provider`, `init_session_journal`, `_AutoTraceMiddleware`, `register_auto_trace_middleware`
- THEN zero matches exist

#### Scenario: server module has no LLMProvider import

- GIVEN `src/nora/server.py` and `src/nora/__main__.py`
- WHEN their imports are scanned for `LLMProvider` and `build_provider`
- THEN zero matches exist
## Cross-References

Depends on `intervention-memory` capability (read-only consumer). The three new `@mcp.tool` registrations (`search_intervention_history`, `get_device_lifecycle_summary`, `correlate_sector_interference`) delegate to pure functions in `nora.intervention_memory.tools`; the dependency direction is one-way (`nora-mcp-server` → `intervention-memory`, never the reverse). Sanitizer contract inherited from `intervention-memory` R9 / `session-journal` R6. Auto-trace recording inherited from `session-journal` R2 — no middleware change was required for this delta.
