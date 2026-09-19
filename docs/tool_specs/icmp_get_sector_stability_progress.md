---
name: icmp_get_sector_stability_progress
description: Poll the current snapshot for an in-flight ICMP sector stability probe (issue #61, PR1 WU-1.5). Tier-0; used by the LLM orchestrator to track progress and to collect more samples after icmp_run_sector_stability_probe.
tier: 0
---

# icmp_get_sector_stability_progress

## Tier

**Tier 0** — Passive Telemetry. The tool only reads the in-process
`RunState` snapshot; it does not touch the radio or the wire. Safe
to invoke at any cadence.

## Operational impact

None. The function reads from the in-memory registry and returns a
JSON-serialisable dict; no network frames, no radio activity.

## Prerequisites

- A live run must exist under the `run_id`. The run is registered by
  `icmp_run_sector_stability_probe` and is GC'd after the registry's
  TTL elapses (default 3600 s).
- The process must have an injected `ProbeRunRegistry` (boot-time
  `set_probe_registry(...)` call in `cli.main()`).

## Inputs

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `run_id` | `string` | yes | 8 hex chars returned by `icmp_run_sector_stability_probe`. |

## Output

JSON object with the same shape as `icmp_run_sector_stability_probe`:

```json
{
  "run_id": "ab12cd34",
  "device_id": "ap-7400-01",
  "ap_host": "192.0.2.10",
  "status": "running",
  "started_at_unix": 1761234567.89,
  "finished_at_unix": null,
  "samples_count": 42,
  "last_samples": [...],
  "last_error": null
}
```

The `last_samples` field caps at 100 entries (the last window of
samples the daemon has flushed) so a long-running poll never ships
the entire buffer.

## Failure modes

- `LookupError` — unknown / expired `run_id`. Raised when the
  registry no longer holds the run (either never existed, or the
  TTL swept it).
