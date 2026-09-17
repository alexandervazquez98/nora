# pmp450i-radio-tools Specification

## Purpose

Defines six new `@mcp.tool` registrations on the global `FastMCP("nora")` instance covering the operational radio-link surface for Cambium PMP 450i networks (issue #15): read summaries (slice 2), unbiased subscriber baseline + per-LUID diagnostics (slice 3), and spectrum analysis + HITL-gated RF migration (slice 4). Slice 4 embeds the approval-token + rollback-watchdog contract mandated by `openspec/config.yaml::rules.specs`.

## Sub-Cluster 1 — Read Summaries (Slice 2)

### Requirement: `snmp_get_ap_summary` + `snmp_get_frame_utilization` — Typed Reads

Both tools SHALL return typed models (`ApSummary`, `FrameUtilization`) and MUST call `OidCatalogRegistry.resolve((vendor, model, firmware))` before any wire frame, applying the major-mismatch / minor-fallback rules from `oid-catalog`. Free-text fields SHALL pass through `Sanitizer.sanitize(...)`; typed scalars SHALL bypass.

#### Scenario: ap_summary returns a typed model

- GIVEN an inventory device with `firmware == "15.2.1"` and a populated agent
- WHEN `snmp_get_ap_summary(device_id="ap-7400-01")` runs
- THEN a typed `ApSummary` is returned

#### Scenario: frame_utilization returns a typed model

- GIVEN a healthy agent
- WHEN the tool runs
- THEN downlink + uplink percentages are returned as typed floats

#### Scenario: unknown OID warns and returns None

- GIVEN a catalog missing one AP-summary OID
- WHEN the tool runs
- THEN a literal warning is emitted AND the missing field is returned as `None`

#### Scenario: minor-mismatch emits the literal warning via the tool path

- GIVEN the agent reports firmware `15.3.1` AND the registry holds `15.2.1` + `15.3.0`
- WHEN `snmp_get_ap_summary` runs
- THEN the catalog at `15.3.0` is used AND stderr carries `"OID catalog fallback: requested 15.3.1, using 15.3.0 (minor mismatch)"`

## Sub-Cluster 2 — Subscriber Baseline (Slice 3)

### Requirement: `categorize_subscribers(...)` — ONE Source Of Truth

`categorize_subscribers(sm_rows, intervention_history) -> Literal["ONLINE_ACTIVE","ACTIVE_DEGRADED","PRE_EXISTING_OFFLINE"]` SHALL be the only categorisation entry point. `PRE_EXISTING_OFFLINE` exclusion MUST be applied first via `known_pre_existing_offline_subscribers` from `search_intervention_history(stage="PRE_DIAGNOSTIC")`.

#### Scenario: ONLINE_ACTIVE / ACTIVE_DEGRADED / PRE_EXISTING_OFFLINE each resolve

- GIVEN three SM rows: one with `session_uptime>0` AND linked modulation; one with `cinr<18`; one whose `luid` is in `known_pre_existing_offline_subscribers`
- WHEN the helper runs
- THEN categories are `ONLINE_ACTIVE`, `ACTIVE_DEGRADED`, `PRE_EXISTING_OFFLINE` respectively AND the PRE_EXISTING_OFFLINE row is excluded from any candidate set

### Requirement: `get_intervention_history_called_before_categorize` Cross-Check

`search_intervention_history(target_ip, stage="PRE_DIAGNOSTIC")` MUST be invoked before `categorize_subscribers(...)` in every caller. The cross-check SHALL fail closed if the sequence is reordered.

#### Scenario: cross-check fails when categorisation runs without history

- GIVEN the intervention-history call is skipped (monkeypatched away)
- WHEN the tool body executes
- THEN a typed error is raised naming the missing prerequisite call

### Requirement: `snmp_get_sm_table` + `snmp_get_sm_detailed_diagnostics` — Typed Reads

`snmp_get_sm_table` SHALL return typed `SubscriberSummary` rows with per-row category. `snmp_get_sm_detailed_diagnostics(luid: str)` SHALL return a typed `SmDetailedDiagnostics` (jitter, CINR, Rx/Tx levels, retransmits, interface error counters).

#### Scenario: unbiased baseline excludes PRE_EXISTING_OFFLINE from candidates

- GIVEN 100 SMs: 60 ONLINE_ACTIVE, 30 ACTIVE_DEGRADED, 10 PRE_EXISTING_OFFLINE
- WHEN `snmp_get_sm_table` runs
- THEN 90 candidate rows are returned AND `pre_existing_offline_count == 10`

#### Scenario: typed diagnostics for one LUID

- GIVEN a valid `luid` for an ONLINE_ACTIVE SM
- WHEN `snmp_get_sm_detailed_diagnostics(luid=...)` runs
- THEN a typed `SmDetailedDiagnostics` is returned

## Sub-Cluster 3 — Spectrum + HITL-Gated Migration (Slice 4)

### Requirement: Approval Token Contract

`snmp_migrate_radio_frequency` SHALL accept `approval_token: str` as the first argument. The stub verifier at `nora.hitl.tokens.verify_approval_token` MUST raise `AutonomousMutationRejected("autonomous device mutation rejected: HITL approval token required")` on missing OR invalid tokens.

#### Scenario: missing or invalid approval_token raises AutonomousMutationRejected

- GIVEN a client invocation WITHOUT `approval_token`, OR with `approval_token="expired-or-bogus"`
- WHEN the tool body executes
- THEN `AutonomousMutationRejected("autonomous device mutation rejected: HITL approval token required")` is raised AND no SNMP SET frame is sent

### Requirement: Rollback Watchdog With Timeout

The migration tool SHALL honour `Settings.nora_hitl_rollback_timeout_seconds` (default `300`). A `threading.Timer` watchdog SHALL confirm management reachability (`snmp_get_ap_summary` over the new channel) within the timeout; loss of reachability triggers revert to the prior carrier frequency.

#### Scenario: watchdog within the timeout

- GIVEN the AP moves to the candidate frequency
- WHEN the watchdog reaches management within the timeout
- THEN no revert runs AND the result reports `{"rolled_back": false}`

#### Scenario: watchdog timeout fires revert

- GIVEN the AP moves to the candidate frequency
- WHEN the watchdog fails to reach management for `>= nora_hitl_rollback_timeout_seconds`
- THEN the prior carrier frequency is restored AND the result reports `{"rolled_back": true, "reason": "loss_of_management"}`

### Requirement: Make-Before-Break Order + PRE_EXISTING_OFFLINE Exclusion

Migration order SHALL be: (1) ONLINE_ACTIVE; (2) ACTIVE_DEGRADED; (3) AP channel change last. PRE_EXISTING_OFFLINE is excluded by `categorize_subscribers`.

#### Scenario: ONLINE_ACTIVE first; AP last; PRE_EXISTING_OFFLINE excluded

- GIVEN 10 ONLINE_ACTIVE, 5 ACTIVE_DEGRADED, 3 PRE_EXISTING_OFFLINE SMs
- WHEN the migration runs
- THEN the order is `[online_active_sm_1..N, active_degraded_sm_1..M, ap_set]` AND `{"pre_existing_offline_excluded": 3}`

### Requirement: Sub-Cluster 3 Sanitizer Contract

Free-text fields SHALL pass through `Sanitizer.sanitize(...)`. Typed scalars (`target_frequency_mhz`, `result["rolled_back"]`, `result["pre_existing_offline_excluded"]`) SHALL bypass.

#### Scenario: free-text masked, typed scalars untouched

- GIVEN a watchdog failure with a free-text reason containing a private IPv4 literal
- WHEN the response is serialised
- THEN the literal is replaced by an alias AND `result["target_frequency_mhz"]` is byte-identical

### Requirement: `snmp_run_spectrum_analysis` — Ranked Clean Frequencies + Maintenance Window

The tool SHALL trigger a spectrum sweep inside the configured maintenance window and rank candidates by lowest measured noise floor. Sweeps outside the window SHALL raise `MaintenanceWindowViolation` and emit no wire frame.

#### Scenario: spectrum returns ranked candidates inside the window

- GIVEN `Settings.nora_maintenance_window_start <= now < Settings.nora_maintenance_window_end`
- WHEN the tool runs
- THEN a ranked list of candidate frequencies is returned

#### Scenario: spectrum refuses outside the window

- GIVEN `now` is outside the maintenance window
- WHEN the tool runs
- THEN `MaintenanceWindowViolation` is raised AND zero SNMP frames are sent

### Requirement: Intervention Record Emission On Migration Completion

On successful migration (with or without rollback), the tool SHALL emit one `save_intervention_record` payload via `nora.intervention_writer.writer.save_intervention_record`. The payload MUST include `stage == "POST_MIGRATION"`, requested + actual frequencies, and per-category SM counts.

#### Scenario: completed migration writes a POST_MIGRATION record (rolled back or not)

- GIVEN the migration completes with `rolled_back in {false, true}`
- WHEN the tool body executes
- THEN `save_intervention_record` is called exactly once with `stage="POST_MIGRATION"` AND the on-disk file is `INT-<ticket>-<ip>-<unix>-<6hex>.json`

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
## Cross-References

`driver-interface`, `oid-catalog`, `intervention-writer`, `nora-mcp-server` (registration guard).

## Covered Named Tests (Slices 2-4 — 18/26)

S2: `ap_summary_returns_typed_model`, `frame_utilization_returns_typed_model`, `unknown_oid_warn_and_value`, `catalog_minor_mismatch_warning_via_summary_call`. S3: `sm_table_categorizes_online_active`, `sm_table_categorizes_active_degraded_low_cinr`, `sm_table_categorizes_pre_existing_offline`, `sm_table_unbiased_baseline_excludes_pre_existing`, `sm_detailed_diagnostics_typed_for_luid`, `get_intervention_history_called_before_categorize`. S4: `spectrum_returns_ranked_clean_frequencies`, `spectrum_respects_maintenance_window`, `migrate_requires_hitl_approval_token`, `migrate_make_before_break_migrates_online_active_first`, `migrate_excludes_pre_existing_offline_subscribers`, `migrate_rolls_back_within_timeout_on_loss_of_management`, `migrate_autonomous_call_raises_autonomous_mutation_rejected`, `migrate_emits_intervention_record_on_completion`.