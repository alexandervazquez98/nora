---
name: icmp_run_sector_stability_probe
description: Start a background ICMP stability probe (RFC 3550 jitter + loss benchmark) against a Cambium PMP 450i AP and all eligible SMs (ONLINE_ACTIVE + ACTIVE_DEGRADED, ordered by LUID). Tier-0 server-side; operator-driven duration 1–30 minutes; no operator_confirmed flag; runs in a daemon thread so FastMCP HTTP transport stays responsive; the run_id is polled via icmp_get_sector_stability_progress.
tier: 0
---

# icmp_run_sector_stability_probe

## Tier

**Tier 0** — Passive Telemetry / Read-Only Active Probe. No SNMP SET
frames emitted; no radio configuration changed. Operationally, a
10-minute continuous ICMP flood (`~5.6 kbps` per sector at 11
destinations × 1 pkt/s × 64 B) is similar in footprint to a
long-running reachability check.

Operational impact: yes — the probe generates network traffic. The
duration is operator-driven (default 10 minutes, max 30 minutes);
the operator can cancel via
`icmp_cancel_sector_stability_probe(run_id)` at any time.

## Inputs

| Name | Type | Required | Default | Bounds | Description |
|------|------|----------|---------|--------|-------------|
| `device_id` | `string` | yes | — | Inventory device id of the AP | |
| `duration_seconds` | `int` | no | 600 | [60, 1800] | Probe duration in seconds |
| `interval_seconds` | `float` | no | 1.0 | > 0 | Per-destination packet cadence |
| `packet_size_bytes` | `int` | no | 64 | > 0 | ICMP payload size |
| `target_luids` | `list[str] \| None` | no | None | — | Optional LUID filter; PRE_EXISTING_OFFLINE LUIDs return in `samples_count_excluded` |
| `collect_window_seconds` | `float` | no | 5.0 | >= 0 | Time the SYNC wrapper blocks to return first samples |

## Output

JSON object with shape (subset):

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

## Failure modes

- `ValueError` — duration / interval / payload out of bounds (raised
  in the SYNC wrapper BEFORE daemon spawn; no wire activity).
- `LookupError` — `icmp_get_sector_stability_progress(run_id)` or
  `icmp_cancel_sector_stability_probe(run_id)` for an unknown /
  expired id.
- `ProbeConfigurationError` — propagated from the daemon thread;
  surfaces in the progress snapshot's `last_error` field.

## Notes

- **Stateful**: each call produces a unique `run_id` (8 hex chars
  via `secrets.token_hex(4)`). Run-state is in-memory; lost on
  process restart (PR2 persistence will harden this).
- **Zero-Leakage**: the response payload includes samples whose
  `target` field carries IPv4 literals (the only way the protocol
  works); the MCP client must redact on display, not the wrapper.
- **Susceptible to `kernel.ping_group_range` misconfiguration**
  (silent zero-samples); PR3 `INSTALL.md` documents the sysctl.
- The daemon owns its own `asyncio` event loop in a separate
  `threading.Thread`; the FastMCP transport's loop never sees the
  probe task.
