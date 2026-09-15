---
name: save_intervention_record
description: Atomically write one intervention record under the configured interventions directory. Tier-2 server-side gate requires a valid HMAC-signed HITL approval token.
tier: 2
requires_hitl_token: true
---

# save_intervention_record

## Tier

**Tier 2** — Service-Affecting Mutation (Write). Records-keeping blast-radius is technically recoverable by deleting the on-disk file, but the record is **part of the audit chain** and therefore MUST be authorised by the operator before any write. The server-side gate enforces the same HMAC token contract as `snmp_migrate_radio_frequency`.

## Operational impact

The library function `nora.intervention_writer.writer.save_intervention_record` is the canonical body — it performs atomic `tmp + fsync + os.replace` so a partial write never replaces the existing file. On-disk free-text is sanitized at write time (W4 mask) so private IPv4 / MAC / hostname literals never reach the file. Validates payload shape, rejects path traversal, rejects duplicate `intervention_id`.

## HITL token contract

Same as `snmp_migrate_radio_frequency`: HMAC-SHA256 over the frozen canonical tuple payload, signed with `Settings.nora_hitl_signing_key`, verified with `hmac.compare_digest`. Legacy stub tokens FAIL.

## Prerequisites

- `Settings.nora_interventions_dir` is configurable (default `./var/interventions/`).
- A valid `payload` dict with `intervention_id` + `timestamp_unix` + `stage` + `target_ip`.
- `Settings.nora_hitl_signing_key` is non-empty (Tier-2 invocation).

## Inputs

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `payload` | `dict` | yes | The intervention record. See `intervention_memory.models.InterventionMemoryRecord`. |

## Outputs

Dict with `status` set to `OK` (success) or one of `INVALID_INPUT` / `INVALID_PAYLOAD` / `PATH_TRAVERSAL_DETECTED` / `DUPLICATE_INTERVENTION_ID` / `WRITE_ERROR`. **Never raises.**

## Failure modes

- `AutonomousMutationRejected` — when the HITL gate fires; surfaces to MCP as a typed error.
- Write-side errors (filesystem) surface as `{"status": "WRITE_ERROR", ...}` without raising.
