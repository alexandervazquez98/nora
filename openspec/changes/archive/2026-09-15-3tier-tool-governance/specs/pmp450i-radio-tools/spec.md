# Delta for pmp450i-radio-tools

## Reconciliation

**MODIFIED** under `openspec/changes/2026-09-15-3tier-tool-governance/`.
Base spec at `openspec/specs/pmp450i-radio-tools/spec.md`. Purely
additive — existing `MaintenanceWindowViolation` semantics preserved.
New `Tier1ClearanceRequired` gate fires **before** the window check
and **before any SNMP GET**, so existing scenarios still hold.

## ADDED Requirements

### Requirement: `snmp_run_spectrum_analysis` Operator Clearance Gate

The tool SHALL accept a new parameter `operator_confirmed: bool = False`
alongside the existing `device_id: str`. When `operator_confirmed is
False` (the default, including the absent-parameter case) the tool
SHALL raise a typed exception derived from `DriverError` (e.g.
`Tier1ClearanceRequired`) **before any SNMP GET frame is emitted**. When
`operator_confirmed is True`, the existing maintenance-window check and
spectrum-sweep logic proceeds unchanged. The new gate is the highest-
priority invariant on this tool.

(Previously: `snmp_run_spectrum_analysis(device_id: str)` — no
operator-clearance parameter; only the maintenance-window gate existed.)

#### Scenario: operator_confirmed=False raises BEFORE any SNMP GET

- GIVEN an MCP client invokes
  `snmp_run_spectrum_analysis(device_id="ap-7400-01")` (no
  `operator_confirmed` argument; default applies)
- WHEN the tool body executes
- THEN `Tier1ClearanceRequired` (or subclass) is raised AND zero
  SNMP GET frames are sent AND the maintenance-window check is
  NOT evaluated

#### Scenario: operator_confirmed=True proceeds inside the maintenance window

- GIVEN `Settings.nora_maintenance_window_start <= now <
  Settings.nora_maintenance_window_end` AND an MCP client invokes
  `snmp_run_spectrum_analysis(device_id="ap-7400-01",
  operator_confirmed=True)`
- WHEN the tool body executes
- THEN a ranked list of candidate frequencies is returned (the
  pre-change Sub-Cluster 3 contract holds)

#### Scenario: absent parameter defaults to False and raises

- GIVEN the FastMCP wire shape for `snmp_run_spectrum_analysis`
  declares `operator_confirmed: bool = False`
- WHEN an MCP client invokes the tool without supplying the
  parameter
- THEN the parameter value resolves to `False` AND the gate raises
  `Tier1ClearanceRequired` AND no wire frame is sent

#### Scenario: operator_confirmed=True OUTSIDE the window still refuses

- GIVEN `now` is outside the maintenance window AND
  `operator_confirmed=True`
- WHEN the tool body executes
- THEN the operator-clearance gate passes AND the existing
  `MaintenanceWindowViolation` invariant still fires (no regression)

### Requirement: Typed Exception Subclass

`Tier1ClearanceRequired` SHALL be a subclass of `DriverError` (mirroring
the existing `MaintenanceWindowViolation` pattern at
`src/nora/drivers/exceptions.py:131-142`). Its message SHALL identify
the tool name AND the missing `operator_confirmed=True` prerequisite so
the LLM-side orchestrator can recover by re-invoking with clearance.

#### Scenario: Tier1ClearanceRequired inherits DriverError

- GIVEN the new exception class
- WHEN `isinstance(exc, DriverError)` is checked
- THEN it returns `True` AND FastMCP surfaces the literal message
  to the MCP client

### Requirement: FastMCP Wire Shape Gains `operator_confirmed`

The `tools/list` JSON-RPC response for `snmp_run_spectrum_analysis`
SHALL declare `operator_confirmed: bool` in its `inputSchema` with a
default of `False`. Existing clients that omit the parameter continue
to receive the typed exception (fail-closed).

#### Scenario: tools/list declares the new field

- GIVEN the server booted via `mcp.run()` over stdio
- WHEN a client calls `tools/list` and inspects the
  `snmp_run_spectrum_analysis` schema
- THEN `inputSchema.properties.operator_confirmed.type == "boolean"`
  AND the default is `false`

## Open Questions (deferred to design)

- **Q1 (composition mechanism):** the orchestrator prompt body must
  reference the operator-clearance protocol. Design decides whether
  the body inlines an `<!-- operator_confirmed -->` marker or uses a
  runtime name-lookup at `@mcp.prompt` call time.
- **Q3 (CLI dispatcher pinning):** the `__main__.py` refactor that
  ships `nora hitl mint` also touches back-compat tests for the
  spectrum gate's no-args path. Design owns the exact refactor.

## Cross-References

- `tool-service-impact-tiers` — Tier-1 governance policy that mandates
  this gate.
- `hitl-approval-tokens` — the HMAC sibling contract for Tier-2 tools
  (this delta is only the Tier-1 half).
- `nora-mcp-server` — FastMCP registration guard must continue to
  accept the new `inputSchema`.