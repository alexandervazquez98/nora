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

The server SHALL register exactly eleven `@mcp.tool`-decorated functions on the global `mcp = FastMCP("nora")`: one driver + three intervention memory + `save_intervention_record` + six radio-link tools (`snmp_get_ap_summary`, `snmp_get_frame_utilization`, `snmp_get_sm_table`, `snmp_get_sm_detailed_diagnostics`, `snmp_run_spectrum_analysis`, `snmp_migrate_radio_frequency`). All eleven names SHALL be re-exported in `__all__`. (Previously: exactly five registrations.)

#### Scenario: server module exports the eleven tool names

- GIVEN `src/nora/server.py` imports the driver + three intervention callables + the writer + six radio-link callables
- WHEN the eleven names are imported from `nora.server`
- THEN the imports succeed AND all eleven names appear in `nora.server.__all__`

#### Scenario: `tools/list` over stdio returns eleven tools in the registered order

- GIVEN the server booted via `mcp.run()` over stdio
- WHEN a client calls `tools/list`
- THEN the response contains the eleven tool names AND their `inputSchema` matches each registered signature
### Requirement: R-NEW-2 — Sanitizer Bound at Tool Boundary

Every free-text field in the eleven tool responses SHALL pass through `Sanitizer.sanitize(...)` before serialisation. Structured top-level fields SHALL bypass. (Previously: four tools; now eleven.)

#### Scenario: free-text fields in the radio-link tools are sanitized

- GIVEN a tool response whose free-text field contains the literal `192.0.2.10`
- WHEN the response is serialised
- THEN the literal is replaced by a synthetic alias AND typed scalars (`carrier_frequency_mhz`, `result["rolled_back"]`) are byte-identical

### Requirement: R-NEW-3 — Hard Read-Only Contract (AST Guard)

The three intervention tools SHALL NOT mutate any file. `tests/intervention_memory/test_no_writes.py` SHALL fail any commit that introduces a writable file operation under `src/nora/intervention_memory/`. The driver tool is out of scope for this guard.

#### ADDED Scenario: the AST read-only guard fails on an injected write

- GIVEN a developer adds `Path("/tmp/x").write_text("x")` inside `src/nora/intervention_memory/storage.py`
- WHEN `uv run pytest tests/intervention_memory/test_no_writes.py` runs
- THEN the test exits non-zero AND the failure message identifies `(storage.py, <line>, "write_text")`

### Requirement: R-NEW-4 — One-Way Cross-Capability Dependency Direction

`src/nora/server.py` MAY import from `nora.intervention_memory.tools`, `nora.intervention_writer.writer`, `nora.drivers.snmp_pmp450i.driver`, `nora.hitl.tokens`, `nora.drivers.interface`, `nora.drivers.resolver`. No module under those packages SHALL import from `nora.server`. (Previously: two consumer packages; now six.)

#### Scenario: consumer packages do not import from `nora.server`

- GIVEN every `.py` under `src/nora/intervention_memory/`, `src/nora/intervention_writer/`, and `src/nora/drivers/`
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
### Requirement: R-NEW-5 — 5th MCP Tool Registration (`save_intervention_record`)

The global `mcp = FastMCP("nora")` instance in `src/nora/server.py` MUST
register a 5th `@mcp.tool`-decorated function named
`save_intervention_record` that delegates 1:1 to
`nora.intervention_writer.writer.save_intervention_record`. The wrapper
MUST be a thin delegate with no logic of its own (mirroring the existing
four read-only wrappers), so the function is callable from non-MCP code
(issue #15 library reuse from `snmp_get_ap_summary`). The 5th name MUST
appear in `nora.server.__all__` after the four existing tool names. The
wrapper MUST NOT require an HITL approval token — record-keeping blast-
radius is recoverable by deleting the file; HITL scope is reserved for #15's
destructive RF migration per `openspec/config.yaml` `rules.specs`. Input
schema, error codes, and security guarantees are defined by the
`intervention-writer` capability (see
`openspec/specs/intervention-writer/spec.md`).

#### Scenario: `save_intervention_record` appears in the registered tool list

- GIVEN the server booted via `mcp.run()` over stdio
- WHEN a client calls `tools/list`
- THEN the response contains `save_intervention_record` AND its `inputSchema` matches the writer's payload contract

#### Scenario: the wrapper delegates 1:1 with no logic drift

- GIVEN `nora.intervention_writer.writer.save_intervention_record` is monkeypatched to return `{"delegated": True}`
- WHEN the MCP wrapper is invoked with a payload
- THEN the patched library function is called exactly once with that payload AND the wrapper returns `{"delegated": True}` unchanged

#### Scenario: stdio call writes a record under the configured dir

- GIVEN `NORA_INTERVENTIONS_DIR=/tmp/test/` AND a valid payload over stdio
- WHEN the MCP tool `save_intervention_record` is invoked
- THEN a `INT-<ticket>-<ip>-<unix>-<hex>.json` file appears under `/tmp/test/` AND the response is `{"status": "OK", "intervention_id": "<stem>"}`

#### Scenario: the wrapper rejects without an HITL token

- GIVEN an MCP client invokes `save_intervention_record` over stdio with no approval token
- WHEN the wrapper executes
- THEN no `HITL_REQUIRED` error is raised AND the record is written immediately
### Requirement: Server-Level `instructions` (Updated) — HITL Advertised

The `instructions` string SHALL advertise that `snmp_migrate_radio_frequency` requires an explicit HITL approval token and that autonomous device mutation is rejected without it. The Zero-Leakage + intervention-memory framing is preserved. (Previously: no destructive tool was exposed; HITL was unadvertised.)

#### Scenario: instructions text names HITL on the migration tool

- GIVEN `mcp = FastMCP("nora", instructions=_SERVER_INSTRUCTIONS)`
- WHEN the instructions string is inspected
- THEN it mentions `snmp_migrate_radio_frequency` AND names the `AutonomousMutationRejected` contract


### Requirement: R-NEW-6 — Tool-Registration Guard (Uncatalogued Tools Rejected)

`src/nora/cli.py` SHALL run a registration guard that walks every function registered against the global `mcp` instance. The guard MUST raise a typed `UncataloguedToolError` if the tool name is absent from `OidCatalogRegistry.REQUIRED_OIDS` for every catalogued `(vendor, model, firmware)` triple. The guard runs ONCE at boot; the rejection MUST abort before `mcp.run(show_banner=False)`. (Issue #24 — ADR #17 Test 10 hardening; the proposal deliberately hardens the original "no se ofrece al LLM" wording into fail-fast registration-time rejection.)

#### Scenario: an uncatalogued `@mcp.tool` is rejected at boot

- GIVEN a developer adds `@mcp.tool def snmp_get_rogue_metric(device_id: str): ...` with no catalog entry
- WHEN `cli.main()` runs the registration guard
- THEN `UncataloguedToolError` is raised naming `(tool_name, reason="no OID catalog entry")` AND `mcp.run()` is never invoked

#### Scenario: every catalogued tool passes the guard

- GIVEN the eleven tool names are all present in the catalog envelope's `"tools"` map
- WHEN the registration guard runs
- THEN it returns success AND `mcp.run(show_banner=False)` proceeds
## Cross-References

Depends on `intervention-memory` capability (read-only consumer). The three new `@mcp.tool` registrations (`search_intervention_history`, `get_device_lifecycle_summary`, `correlate_sector_interference`) delegate to pure functions in `nora.intervention_memory.tools`; the dependency direction is one-way (`nora-mcp-server` → `intervention-memory`, never the reverse). Sanitizer contract inherited from `intervention-memory` R9 / `session-journal` R6. Auto-trace recording inherited from `session-journal` R2 — no middleware change was required for this delta.
