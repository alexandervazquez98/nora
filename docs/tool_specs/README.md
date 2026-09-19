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

## Tool-to-tier mapping (issue #43)

### Tier 0 — Passive Telemetry (Read-Only)

Direct execution; orchestrator may invoke immediately. No operator interaction, no HITL gate, no maintenance-window dependency beyond what the existing slice-1 / slice-2 / slice-3 contracts pin.

| Tool | Spec |
|------|------|
| `snmp_get_ap_summary` | [snmp_get_ap_summary.md](./snmp_get_ap_summary.md) |
| `snmp_get_sm_table` | [snmp_get_sm_table.md](./snmp_get_sm_table.md) |
| `snmp_get_pmp450i_radio_metrics` | [snmp_get_pmp450i_radio_metrics.md](./snmp_get_pmp450i_radio_metrics.md) |
| `snmp_get_frame_utilization` | [snmp_get_frame_utilization.md](./snmp_get_frame_utilization.md) |
| `snmp_get_sm_detailed_diagnostics` | [snmp_get_sm_detailed_diagnostics.md](./snmp_get_sm_detailed_diagnostics.md) |
| `search_intervention_history` | [search_intervention_history.md](./search_intervention_history.md) |
| `get_device_lifecycle_summary` | [get_device_lifecycle_summary.md](./get_device_lifecycle_summary.md) |
| `correlate_sector_interference` | [correlate_sector_interference.md](./correlate_sector_interference.md) |
| `icmp_run_sector_stability_probe` | [icmp_run_sector_stability_probe.md](./icmp_run_sector_stability_probe.md) |
| `icmp_get_sector_stability_progress` | [icmp_get_sector_stability_progress.md](./icmp_get_sector_stability_progress.md) |
| `icmp_cancel_sector_stability_probe` | [icmp_cancel_sector_stability_probe.md](./icmp_cancel_sector_stability_probe.md) |
| `icmp_list_probe_runs` | [icmp_list_probe_runs.md](./icmp_list_probe_runs.md) |

### Tier 1 — Potentially Disruptive / Active Telemetry

Pause & Clearance Gate — agent MUST explain necessity and request operator clearance. Server-side gate refuses any call that arrives without `operator_confirmed=True`.

| Tool | Spec |
|------|------|
| `snmp_run_spectrum_analysis` | [snmp_run_spectrum_analysis.md](./snmp_run_spectrum_analysis.md) |

### Tier 2 — Service-Affecting Mutations (Write / Config)

Strict HITL Gate — halt. Valid ticket + maintenance window + cryptographic HITL token.

| Tool | Spec |
|------|------|
| `snmp_migrate_radio_frequency` | [snmp_migrate_radio_frequency.md](./snmp_migrate_radio_frequency.md) |
| `save_intervention_record` | [save_intervention_record.md](./save_intervention_record.md) |

## ADR-4 cross-validator invariants

The `ToolSpecValidator` enforces these invariants at scan time:

- `tier ∈ {0, 1, 2}` (else the spec is dropped silently and `get(name)` raises `PromptNotFoundError`).
- `tier == 1 ⇒ requires_operator_confirmed: True` (required).
- `tier == 2 ⇒ requires_hitl_token: True` (required).
- `tier == 0 ⇒ both flags MAY be false or absent`.
- README.md is exempt from tier validation (it is not a tool).

## Composition

The orchestrator prompt body at `src/nora/prompts/netops_orchestrator.md` references each tier and tool by name; the LLM resolves tool → spec via the composed prompt. `PromptRegistry.from_settings(settings)` scans both `src/nora/prompts/*.md` AND `Settings.nora_tool_specs_dir` so the orchestrator and the tool specs are composed at boot.
