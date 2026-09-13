# Delta for nora-mcp-server

> Modified by `2026-09-13-pmp450i-production-surface` — tool surface grows from 5 to 11 (existing 5 + 6 new from slices 2-4); `instructions` advertises HITL requirement on `snmp_migrate_radio_frequency`; tool-registration guard from `oid-catalog-integration` applies at `@mcp.tool()` time.

## MODIFIED Requirements

### Requirement: R-NEW-1 — `@mcp.tool` Registrations (Updated Count)

The server SHALL register exactly eleven `@mcp.tool`-decorated functions on the global `mcp = FastMCP("nora")`: one driver + three intervention memory + `save_intervention_record` + six radio-link tools (`snmp_get_ap_summary`, `snmp_get_frame_utilization`, `snmp_get_sm_table`, `snmp_get_sm_detailed_diagnostics`, `snmp_run_spectrum_analysis`, `snmp_migrate_radio_frequency`). All eleven names SHALL be re-exported in `__all__`. (Previously: exactly five registrations.)

#### Scenario: server module exports the eleven tool names

- GIVEN `src/nora/server.py` imports the driver + three intervention callables + the writer + six radio-link callables
- WHEN the eleven names are imported from `nora.server`
- THEN the imports succeed AND all eleven names appear in `nora.server.__all__`

#### Scenario: `tools/list` over stdio returns eleven tools in the registered order

- GIVEN the server booted via `mcp.run()` over stdio
- WHEN a client calls `tools/list`
- THEN the response contains the eleven tool names AND their `inputSchema` matches each registered signature

### Requirement: Server-Level `instructions` (Updated) — HITL Advertised

The `instructions` string SHALL advertise that `snmp_migrate_radio_frequency` requires an explicit HITL approval token and that autonomous device mutation is rejected without it. The Zero-Leakage + intervention-memory framing is preserved. (Previously: no destructive tool was exposed; HITL was unadvertised.)

#### Scenario: instructions text names HITL on the migration tool

- GIVEN `mcp = FastMCP("nora", instructions=_SERVER_INSTRUCTIONS)`
- WHEN the instructions string is inspected
- THEN it mentions `snmp_migrate_radio_frequency` AND names the `AutonomousMutationRejected` contract

### Requirement: R-NEW-2 — Sanitizer Bound At Tool Boundary (Updated Count)

Every free-text field in the eleven tool responses SHALL pass through `Sanitizer.sanitize(...)` before serialisation. Structured top-level fields SHALL bypass. (Previously: four tools; now eleven.)

#### Scenario: free-text fields in the radio-link tools are sanitized

- GIVEN a tool response whose free-text field contains the literal `192.0.2.10`
- WHEN the response is serialised
- THEN the literal is replaced by a synthetic alias AND typed scalars (`carrier_frequency_mhz`, `result["rolled_back"]`) are byte-identical

### Requirement: R-NEW-4 — One-Way Cross-Capability Dependency Direction (Updated)

`src/nora/server.py` MAY import from `nora.intervention_memory.tools`, `nora.intervention_writer.writer`, `nora.drivers.snmp_pmp450i.driver`, `nora.hitl.tokens`, `nora.drivers.interface`, `nora.drivers.resolver`. No module under those packages SHALL import from `nora.server`. (Previously: two consumer packages; now six.)

#### Scenario: consumer packages do not import from `nora.server`

- GIVEN every `.py` under `src/nora/intervention_memory/`, `src/nora/intervention_writer/`, and `src/nora/drivers/`
- WHEN a static grep scans for `from nora.server` or `import nora.server`
- THEN zero matches are found

## ADDED Requirements

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