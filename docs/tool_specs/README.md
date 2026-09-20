---
name: README
description: Index of NORA MCP tool specifications under the ADR-4 frozen schema.
---

# NORA MCP Tool Specifications

This directory contains one tool-spec file per NORA MCP tool, authored against the frozen ADR-4 front-matter schema:

```yaml
---
name: <tool_name>                 # MUST match filename stem
description: <non-empty>
tier: 0 | 1 | 2                   # required for tool specs (NOT for README)
requires_operator_confirmed: bool # required when tier == 1
requires_hitl_token: bool         # required when tier == 2
---
```

## Authoring Workflow (mandatory when adding a new tool)

The MCP server enforces a strict contract: every `@mcp.tool` registered in `src/nora/server.py` MUST have a companion tool-spec file under `docs/tool_specs/`, and that file MUST be reachable at runtime via `get_prompt(name="<tool>")` (per `prompt-registry` spec, *Per-Tool MCP Prompt Exposure* requirement). The boot-time cross-validator refuses specs that violate the schema; a missing or malformed spec aborts the server start with `PromptNotFoundError`.

When adding a new tool, follow these steps in order:

1. **Implement the tool.** Add `@mcp.tool def <tool>(...)` in `src/nora/server.py` with the typed signature, governance gate (`operator_confirmed` / `approval_token` where applicable), and Zero-Leakage sanitiser coverage.
2. **Choose the tier.** Read [`openspec/specs/tool-service-impact-tiers/spec.md`](../../openspec/specs/tool-service-impact-tiers/spec.md) and pick the tier that matches the tool's wire-level impact. The three tiers are:
   - **Tier 0** — read-only / passive telemetry; no operator gate.
   - **Tier 1** — active telemetry or potentially disruptive probe; server-side `operator_confirmed=True` gate.
   - **Tier 2** — service-affecting mutation; cryptographic HITL approval token required.
3. **Author the spec file.** Create `docs/tool_specs/<tool>.md` with the front-matter schema above and a Markdown body that documents:
   - **Tier** — restate the tier and the governance protocol it implies.
   - **Operational impact** — what the tool actually does on the wire; what state changes; what failure modes.
   - **Parameter contract** — exact field names, types, required-vs-optional, defaults.
   - **Preconditions** — registered device, maintenance window, pre-flight results, etc.
   - **Side effects and rollback** — what happens after success; how an operator reverts the change if needed.
   - **Cross-references** — link to the related driver spec, ADR, and orchestrator prompt section.
4. **Wire the MCP prompt exposure.** Add a thin `@mcp.prompt(name="<tool>")` wrapper in `src/nora/server.py` next to the existing 15 (see `_EXPOSED_PROMPTS`). Use the `name=` keyword to avoid collision with the `@mcp.tool` of the same name. Add the new name to `_EXPOSED_PROMPTS` to keep the audit allow-list in sync.
5. **Register the tool in the orchestrator prompt.** Add a one-line entry to the appropriate tier list in §6 of `src/nora/prompts/netops_orchestrator.md` and a row to the tier table below.
6. **Run the full test suite.** The cross-validator tests in `tests/test_prompts.py` will fail loudly if any of the steps above is wrong:
   - `test_every_tool_spec_declares_tier` — front-matter completeness.
   - `test_tool_spec_tier_1_requires_operator_confirmed_cross_validator` / `test_tool_spec_tier_2_requires_hitl_token_cross_validator` — tier-conditional invariants.
   - `test_server_exposes_all_tool_spec_prompts` — `@mcp.prompt` exposure completeness.
   - `test_exposed_prompts_allowlist_matches_mcp_list_prompts` — `_EXPOSED_PROMPTS` allow-list in sync.
   - `test_orchestrator_prompt_names_all_shipped_tool_specs` — orchestrator §6 references the new tool.

## Tool-to-tier mapping (issue #43)

### Tier 0 — Passive Telemetry (Read-Only)

Direct execution; orchestrator may invoke immediately. No operator interaction, no HITL gate, no maintenance-window dependency beyond what the existing slice-1 / slice-2 / slice-3 contracts pin.

| Tool | Spec |
|------|------|
| `correlate_sector_interference` | [correlate_sector_interference.md](./correlate_sector_interference.md) |
| `get_device_lifecycle_summary` | [get_device_lifecycle_summary.md](./get_device_lifecycle_summary.md) |
| `icmp_list_probe_runs` | [icmp_list_probe_runs.md](./icmp_list_probe_runs.md) |
| `search_intervention_history` | [search_intervention_history.md](./search_intervention_history.md) |
| `snmp_get_ap_summary` | [snmp_get_ap_summary.md](./snmp_get_ap_summary.md) |
| `snmp_get_frame_utilization` | [snmp_get_frame_utilization.md](./snmp_get_frame_utilization.md) |
| `snmp_get_pmp450i_radio_metrics` | [snmp_get_pmp450i_radio_metrics.md](./snmp_get_pmp450i_radio_metrics.md) |
| `snmp_get_sm_detailed_diagnostics` | [snmp_get_sm_detailed_diagnostics.md](./snmp_get_sm_detailed_diagnostics.md) |
| `snmp_get_sm_table` | [snmp_get_sm_table.md](./snmp_get_sm_table.md) |

### Tier 1 — Potentially Disruptive / Active Telemetry

Pause & Clearance Gate — agent MUST explain necessity and request operator clearance. Server-side gate refuses any call that arrives without `operator_confirmed=True`.

| Tool | Spec |
|------|------|
| `snmp_run_spectrum_analysis` | [snmp_run_spectrum_analysis.md](./snmp_run_spectrum_analysis.md) |

### Tier 2 — Service-Affecting Mutations (Write / Config)

Strict HITL Gate — halt. Valid ticket + maintenance window + cryptographic HITL token.

| Tool | Spec |
|------|------|
| `save_intervention_record` | [save_intervention_record.md](./save_intervention_record.md) |
| `snmp_migrate_radio_frequency` | [snmp_migrate_radio_frequency.md](./snmp_migrate_radio_frequency.md) |
| `snmp_reboot_radio` | [snmp_reboot_radio.md](./snmp_reboot_radio.md) |

## ADR-4 cross-validator invariants

The `ToolSpecValidator` enforces these invariants at scan time:

- `tier ∈ {0, 1, 2}` (else the spec is dropped silently and `get(name)` raises `PromptNotFoundError`).
- `tier == 1 ⇒ requires_operator_confirmed: True` (required).
- `tier == 2 ⇒ requires_hitl_token: True` (required).
- `tier == 0 ⇒ both flags MAY be false or absent`.
- README.md is exempt from tier validation (it is not a tool).

## Composition

The orchestrator prompt body at `src/nora/prompts/netops_orchestrator.md` references each tier and tool by name (§6: tier classification) and instructs the LLM to call `get_prompt(name="<tool>")` before invoking any tool (§7: Spec Lookup Protocol). The LLM resolves tool → spec via that runtime lookup, not via inline composition of all 13 spec bodies into the system prompt.

`PromptRegistry.from_settings(settings)` scans both `src/nora/prompts/*.md` AND `Settings.nora_tool_specs_dir` so the orchestrator and the tool specs are loaded at boot. Boot is fail-closed: any spec that violates the schema (missing front-matter, bad tier, tier-conditional flag absent) raises `PromptNotFoundError` and the server does not start.
