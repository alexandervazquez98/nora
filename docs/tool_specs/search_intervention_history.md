---
name: search_intervention_history
description: Read-only search of on-disk intervention history (filtered by target_ip, ticket_number, stage, keyword) with sanitised free-text fields.
tier: 0
---

# search_intervention_history

## Tier

**Tier 0** — Passive Telemetry (Read-Only). Reads the on-disk intervention-history directory; never writes.

## Operational impact

Filters (AND-combined): `target_ip` (exact), `ticket_number` (substring), `stage` (case-insensitive equality), `keyword` (substring on serialised record). Results sorted by `timestamp_unix` DESC and capped to `limit`. Every free-text field is sanitized through `Sanitizer.sanitize(...)`; structured top-level fields bypass per the existing session-journal R6 contract.

This tool is read-only — NORA never writes to the interventions directory. The hard read-only rule is enforced structurally by an AST scan under `tests/intervention_memory/test_no_writes.py`.

## Prerequisites

- `Settings.nora_interventions_dir` must point at an existing, readable directory.

## Inputs

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `target_ip` | `string \| null` | no | Exact-match filter. |
| `ticket_number` | `string \| null` | no | Substring filter. |
| `stage` | `string \| null` | no | Case-insensitive equality filter. |
| `keyword` | `string \| null` | no | Substring filter on serialised record. |
| `limit` | `int` | no | Cap on returned records (default 5). |

## Outputs

`list[dict]` — at most `limit` records, sorted by `timestamp_unix` DESC.

## Failure modes

- Disk-read errors surface as empty results; no wire frames involved.
