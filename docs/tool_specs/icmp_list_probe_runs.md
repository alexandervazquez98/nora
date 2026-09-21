---
name: icmp_list_probe_runs
description: List recent probe-run records from the on-disk registry (issue #61, PR3 WU-3.7). Tier-0; used by the LLM orchestrator to enumerate historical ICMP sector stability probes.
tier: 0
---

# icmp_list_probe_runs

## Tier

**Tier 0** — Passive Telemetry. The tool only reads on-disk probe
records under `<settings.nora_probe_results_dir>/`; it does not
touch the radio or the wire. Safe to invoke at any cadence.

## Operational impact

None. The function reads `PRB-*.json` files, validates each against
the `ProbeRunRecord` model, applies the Sanitizer to the metadata,
and returns the most recent `limit` records sorted by
`started_at_unix` descending. No network frames, no radio activity,
no state mutation.

## Prerequisites

- The probe-results directory must be reachable. By default the
  path is `<process-cwd>/var/probes/` (overridable via
  `NORA_PROBE_RESULTS_DIR`).
- Files that fail validation (corrupt JSON, missing fields) are
  skipped and counted in `errors_skipped` — never raises to MCP.

## Inputs

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `limit` | `int` | no | Maximum records to return. Default 20. |

## Output

JSON object:

```json
{
  "runs": [list of dicts],
  "total_files_scanned": 42,
  "errors_skipped": 0
}
```

Each run dict has: `run_id`, `sector`, `device_id` (sanitized),
`started_at_unix`, `finished_at_unix`, `verdict.sector_verdict`,
`verdict.rationale`, `samples_count`, `pdf_path`.

## Failure modes

- `OSError` if the probe-results directory is unreachable. Raised
  with the OS error message; the orchestrator surfaces it to the
  operator.
- Files that fail JSON parsing or model validation are skipped,
  not raised. The skip count is exposed in `errors_skipped`.