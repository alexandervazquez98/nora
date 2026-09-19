---
name: icmp_cancel_sector_stability_probe
description: Cancel an in-flight ICMP sector stability probe (issue #61, PR1 WU-1.5). Tier-0; sets the run state to CANCELLED so the daemon exits cleanly on its next sample flush.
tier: 0
---

# icmp_cancel_sector_stability_probe

## Tier

**Tier 0** — Passive Telemetry. The function only mutates an
in-process status flag; the wire-level impact is the daemon's exit
on its next sample flush (the per-target pingers close in the
`finally:` clause of `run_probe`).

## Operational impact

Negligible. The daemon sees the status flip on its next 50 ms
sample flush, exits its `async for loop`, and closes every per-destination
pinger. No SNMP SET frames are emitted; no radio configuration is
touched.

## Prerequisites

- A live run must exist under the `run_id`. The run is registered by
  `icmp_run_sector_stability_probe` and is GC'd after the registry's
  TTL elapses (default 3600 s).

## Inputs

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `run_id` | `string` | yes | 8 hex chars returned by `icmp_run_sector_stability_probe`. |

## Output

```json
{"run_id": "ab12cd34", "status": "cancelled"}
```

The response is returned synchronously after the status flip;
the daemon may take up to one sample interval (default 1 s) to
finish closing its pingers.

## Failure modes

- `LookupError` — unknown / expired `run_id`.
