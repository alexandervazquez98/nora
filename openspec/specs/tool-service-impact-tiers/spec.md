# tool-service-impact-tiers Specification

## Purpose

Canonical reference for NORA MCP's three-tier Service-Impact taxonomy
(0 / 1 / 2), per-tier governance policy, and tool-to-tier mapping.
Closes the policy half of issue #43; cryptographic half lives in
`hitl-approval-tokens`.

## Reconciliation

**NEW**. No prior spec at `openspec/specs/tool-service-impact-tiers/`.
User brief name reconciled to proposal canonical name.

## Requirements

### Requirement: Three-Tier Taxonomy Is Frozen

Every NORA MCP tool SHALL be classified into exactly one tier per
proposal §"Tier classification": **Tier 0 (Passive Telemetry)**,
**Tier 1 (Potentially Disruptive)**, **Tier 2 (Service-Affecting
Mutations)**. A fourth tier is forbidden.

#### Scenario: every registered tool is classified into exactly one tier

- GIVEN the eleven tools registered against the global `mcp` instance
- WHEN the tool-to-tier mapping table is read
- THEN each tool appears exactly once AND each tier value is in
  `{0, 1, 2}`

### Requirement: Tool-To-Tier Mapping Is Verbatim

Mapping matches proposal §"Tier classification" verbatim.
**Tier 0** (8): `snmp_get_ap_summary`, `snmp_get_sm_table`,
`snmp_get_pmp450i_radio_metrics`, `snmp_get_frame_utilization`,
`snmp_get_sm_detailed_diagnostics`, `search_intervention_history`,
`get_device_lifecycle_summary`, `correlate_sector_interference`.
**Tier 1** (1): `snmp_run_spectrum_analysis`. **Tier 2** (2):
`snmp_migrate_radio_frequency`, `save_intervention_record`.

#### Scenario: registry enumeration matches the mapping table

- GIVEN `mcp = FastMCP("nora")` boots and `tools/list` returns the
  eleven registered tools
- WHEN each tool name is looked up in the mapping
- THEN every tool resolves to one tier AND no tier is empty

### Requirement: Tier 0 — Direct Execution

A Tier 0 tool SHALL be invokable without operator interaction; no
gate beyond the existing catalogue-sanitizer-error surface.

#### Scenario: Tier 0 tool runs without an approval token

- GIVEN an MCP client invokes `snmp_get_ap_summary(device_id)`
- WHEN the tool body executes
- THEN no clearance prompt is required AND no
  `AutonomousMutationRejected` is raised

### Requirement: Tier 1 — Pause & Clearance Gate

A Tier 1 tool SHALL require explicit `operator_confirmed: bool = False`
on its wire schema. On `False` (or absent) the tool SHALL raise a
typed `DriverError` subclass (e.g. `Tier1ClearanceRequired`) **before
any wire frame**.

#### Scenario: Tier 1 tool refuses operator_confirmed=False

- GIVEN an MCP client invokes `snmp_run_spectrum_analysis(device_id)`
  WITHOUT `operator_confirmed` (default `False`)
- WHEN the tool body executes
- THEN a typed `Tier1ClearanceRequired` exception is raised AND zero
  wire frames are sent

#### Scenario: Tier 1 tool proceeds only after explicit clearance

- GIVEN an MCP client invokes
  `snmp_run_spectrum_analysis(device_id, operator_confirmed=True)`
- WHEN the tool body executes
- THEN the existing maintenance-window + spectrum-sweep logic
  proceeds

### Requirement: Tier 2 — Strict HITL Gate

A Tier 2 tool SHALL refuse to mutate state unless the caller supplies
a valid `HitlApprovalToken` whose HMAC verifies via
`hmac.compare_digest`. On failure: `AutonomousMutationRejected` with
the literal message
`"autonomous device mutation rejected: HITL approval token required"`.

#### Scenario: Tier 2 tool rejects forged token

- GIVEN an MCP client invokes `snmp_migrate_radio_frequency(...)`
  with a token whose `signature` was computed under a different key
- WHEN `verify_approval_token` runs
- THEN `AutonomousMutationRejected` is raised with the literal
  message AND no SNMP SET frame is sent

#### Scenario: Tier 2 tool accepts a legitimate HMAC token

- GIVEN `nora hitl mint --operator-id alice --ttl-seconds 900` emits
  a signed token AND the same operator-id is supplied within the TTL
- WHEN the migration tool runs
- THEN `verify_approval_token` returns the typed token AND the SNMP
  SET proceeds

### Requirement: Modular Tool Specs Surface

`docs/tool_specs/<tool_name>.md` SHALL exist per tool PLUS a
`README.md` (twelve files). Each SHALL carry YAML front-matter
`tier: 0 | 1 | 2`. Extended schema is **OPEN QUESTION 4**.

#### Scenario: every tool has a matching spec file

- GIVEN the tool registry and `docs/tool_specs/`
- WHEN each tool name resolves to `<tool_name>.md`
- THEN one file exists per tool AND a `README.md` exists

#### Scenario: front-matter tier marker agrees with the mapping

- GIVEN a `docs/tool_specs/<tool_name>.md` with `tier: N` front-matter
- WHEN `<tool_name>` is looked up in the mapping table
- THEN `N` equals the mapping's tier

## Open Questions (deferred to design)

**Q1** — composition mechanism. **Q4** — front-matter schema.

## Cross-References

`pmp450i-radio-tools`, `hitl-approval-tokens`, `prompt-registry`,
`nora-mcp-server`, `secure-configuration`.