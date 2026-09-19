---
name: snmp_migrate_radio_frequency
description: Migrate a PMP 450i AP to a target carrier frequency. Tier-2 server-side gate requires a valid HMAC-signed HITL approval token BEFORE any wire frame.
tier: 2
requires_hitl_token: true
---

# snmp_migrate_radio_frequency

## Tier

**Tier 2** — Service-Affecting Mutation (Write / Config). On the real radio the AP carrier SET frame drops SMs and alters carrier frequency; the strict HITL gate enforces operator clearance AND a valid HMAC-signed token minted via `nora hitl mint`. The token verifier uses `hmac.compare_digest` for constant-time comparison (timing-attack resistance).

## Operational impact

Make-before-break order (ONLINE_ACTIVE → ACTIVE_DEGRADED → AP carrier) with `PRE_EXISTING_OFFLINE` SMs excluded. A `threading.Timer` watchdog armed after the AP SET frame reverts the SET frame on loss-of-management. The tool emits exactly one `POST_MIGRATION` intervention record per completion (success or rollback).

## HITL token contract

- The token is HMAC-SHA256-signed over the frozen canonical tuple payload
  `f"{operator_id}|{issued_at_iso8601}|{expires_at_iso8601}|{token}"` (see `nora.hitl.tokens`).
- The signing key is sourced from `Settings.nora_hitl_signing_key: SecretStr` — lazy fail-closed when empty.
- Legacy stub tokens (no `signature` field) FAIL verification with `AutonomousMutationRejected`.
- The literal error message `autonomous device mutation rejected: HITL approval token required` is the contract seam.

## Prerequisites

- `device_id` must resolve in `Inventory`.
- The catalog entry for `(vendor, model, firmware)` must include the migration OID branch (`migrateCarrierFrequency`, `migratePriorCarrierFrequency`).
- A valid `approval_token` produced by `nora hitl mint --operator-id <id> --ttl-seconds <n>`.
- `Settings.nora_hitl_signing_key` is non-empty.

## Inputs

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `device_id` | `string` | yes | Inventory device id (the AP). |
| `approval_token` | `string` | yes | JSON-encoded `HitlApprovalToken` minted by `nora hitl mint`. |
| `target_frequency_mhz` | `float` | yes | Requested carrier frequency in MHz. |
| `sm_communities` | `dict[str, str] \| null` | no | Per-SM community overrides for heterogeneous sectors (issue #62 WU-4). Keys may be SM **IP addresses** OR **LUIDs**; resolution order is **IP first, then LUID, then inventory** (operator decision 2026-09-19 — the MCP tool emits IP-targeted SETs, so IP is the natural key). `None` or `{}` falls back to the legacy behaviour (inventory communities only). Each override replaces the SM's inventory community for the wire path only — inventory state is NOT mutated. |

## Outputs

Typed dict matching `MigrationResult`: `rolled_back`, `reason`, `pre_existing_offline_excluded`, `online_active_migrated`, `active_degraded_migrated`, `target_frequency_mhz`, `device_id`, `sm_community_overrides_used`, `band_crossing`.

`sm_community_overrides_used` (int) aggregates how many SMs were migrated with non-inventory credentials; useful for audit and for the orchestrator to surface a per-migration summary. The resolved community **string** itself never travels to the intervention record — only the per-SM `community_source` label (`OVERRIDE_IP` / `OVERRIDE_LUID` / `INVENTORY`) does.

## Failure modes

- `AutonomousMutationRejected` — missing / invalid / expired / kill-switched token, or signature mismatch. **Zero wire frames.**
- `DeviceNotFoundError` — unknown `device_id`.
- `CatalogNotFoundError` — missing catalog.
- `NetworkUnreachableError` / `SnmpTimeoutError` — wire failure.
