---
name: correlate_sector_interference
description: Detect co-channel and adjacent-channel interference on a tower for a candidate target frequency.
tier: 0
---

# correlate_sector_interference

## Tier

**Tier 0** — Passive Telemetry (Read-Only). Walks on-disk intervention records to detect frequency collisions; never writes.

## Operational impact

Walks the most-recent `Settings.nora_interventions_correlate_scan_limit` records (default 50) and reports every record whose `system_name` contains `tower_name` (substring, case-insensitive) AND whose `carrier_frequency_mhz` is within `channel_width_mhz` of `target_frequency_mhz`. Each conflict is classified `CO_CHANNEL` (delta < 0.5 MHz) or `ADJACENT_CHANNEL`.

Documented caveat: substring tower match false-positives on short prefixes (e.g., `tower_name="A"` matches every `*-A` AP). A structured `tower` field is the future fix.

## Prerequisites

- `Settings.nora_interventions_dir` must point at an existing, readable directory.

## Inputs

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `tower_name` | `string` | yes | Substring (case-insensitive) on `system_name`. |
| `target_frequency_mhz` | `float` | yes | Candidate carrier frequency in MHz. |
| `channel_width_mhz` | `float` | no | Channel-width window (default 20.0). |

## Outputs

`list[dict]` — at most the configured scan limit; each entry classified `CO_CHANNEL` or `ADJACENT_CHANNEL`.

## Failure modes

- Disk-read errors surface as empty results; no wire frames involved.
