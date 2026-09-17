---
name: get_device_lifecycle_summary
description: Read the lifecycle summary for one device (previous tickets, recorded stages, latest intervention, known pre-existing offline subscribers).
tier: 0
---

# get_device_lifecycle_summary

## Tier

**Tier 0** — Passive Telemetry (Read-Only). Aggregates on-disk intervention history into a typed lifecycle summary; no write frames.

## Operational impact

Algorithm: `search_intervention_history(target_ip=target_ip, limit=20)`. Returns `{"status": "NO_HISTORY_FOUND", ...}` when no records match; otherwise a SUCCESS dict with `total_recorded_interventions`, `associated_tickets`, `stages_recorded`, `latest_intervention`, and `known_pre_existing_offline_subscribers` (extracted from the most recent `PRE_DIAGNOSTIC` record). All free-text fields sanitized on read; structured fields bypass.

## Prerequisites

- `Settings.nora_interventions_dir` must point at an existing, readable directory.

## Inputs

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `target_ip` | `string` | yes | The device IP (RFC 5737 doc ranges only). |

## Outputs

Typed dict with `status`, `total_recorded_interventions`, `associated_tickets`, `stages_recorded`, `latest_intervention`, and `known_pre_existing_offline_subscribers`.

## Failure modes

- Disk-read errors surface as `{"status": "NO_HISTORY_FOUND"}`; no wire frames involved.
