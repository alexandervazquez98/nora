---
name: snmp_reboot_radio
description: Reboot a PMP 450i radio after a band-class-crossing frequency migration. Tier-2 server-side gate requires a valid HMAC-signed HITL approval token BEFORE any wire frame.
tier: 2
requires_hitl_token: true
---

# snmp_reboot_radio

## Tier

**Tier 2** — Service-Affecting Mutation (Write / Config). On the real radio the SET frame on `whispBoxControls 2` triggers a full reboot cycle that drops every subscriber on the sector for ~90-120 seconds. Two HITL tokens are required in the band-crossing flow: one for the migration (`snmp_migrate_radio_frequency`) and one for this reboot.

## Operational impact

The reboot is independently HITL-gated; the orchestrator MUST NOT chain `snmp_reboot_radio` after `snmp_migrate_radio_frequency` without minting a fresh token. The migration tool's HITL token is single-use.

The helper reads the radio's `rebootIfRequired` OID (`1.3.6.1.4.1.161.19.3.3.3.4.0`, `whispBoxControls 4`) FIRST. When the firmware votes `rebootNotRequired(0)` the helper returns `rebooted=False, reason='not_required_by_firmware'` WITHOUT emitting any SET frame. The firmware's authoritative vote wins over the table-driven band-crossing detector — see the feature doc for the rationale.

When the firmware votes `rebootRequired(1)` (or the read fails — fail-closed) the helper emits the SET on `reboot` (`1.3.6.1.4.1.161.19.3.3.3.2.0`, `whispBoxControls 2`) with value `fullReboot(2)`. Per the 25.x MIB DESCRIPTION: "Setting the variable to 1 will reboot the unit. Setting the variable to 2 will perform a full reboot of a 450i radio, while performing a normal reboot on other radios." 450i is the only model in scope today.

Per the WU-3 (`pr44-followups.md`) pattern: when the SNMP client lacks `apply_oid` — e.g. the production `V2CClient` whose read-only `SnmpClient` Protocol is deliberate — the tool short-circuits before any wire frame and returns a typed dry-run result (`dry_run=True, would_set=[(reboot_oid, 2)]`) so operators see what WOULD have happened, instead of raising `AttributeError`. Write mutations stay out of scope.

The tool emits exactly one `POST_REBOOT` intervention record per completion (rebooted / skipped / dry-run), mirroring the slice-4 contract for `MigrationResult` / `POST_MIGRATION`.

## HITL token contract

- The token is HMAC-SHA256-signed over the frozen canonical tuple payload `f"{operator_id}|{issued_at_iso8601}|{expires_at_iso8601}|{token}"` (see `nora.hitl.tokens`).
- The signing key is sourced from `Settings.nora_hitl_signing_key: SecretStr` — lazy fail-closed when empty.
- Legacy stub tokens (no `signature` field) FAIL verification with `AutonomousMutationRejected`.
- The literal error message `autonomous device mutation rejected: HITL approval token required` is the contract seam.

## Prerequisites

- `device_id` must resolve in `Inventory`.
- The catalog entry for `(vendor, model, firmware)` must include both `reboot` and `rebootIfRequired` OID names (catalog v2 ships both).
- A valid `approval_token` produced by `nora hitl mint --operator-id <id> --ttl-seconds <n>`.
- `Settings.nora_hitl_signing_key` is non-empty.
- The orchestrator has surfaced the pre-flight community validation result for the AP and the operator has confirmed the migration.

## Inputs

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `device_id` | `string` | yes | Inventory device id (the AP). |
| `approval_token` | `string` | yes | JSON-encoded `HitlApprovalToken` minted by `nora hitl mint`. |

## Outputs

Typed dict matching `RebootResult`: `device_id`, `rebooted`, `reason`, `firmware_vote`, `dry_run`, `would_set`, `set_calls`, `expected_recovery_seconds`, `hitl_required`.

## Failure modes

- `AutonomousMutationRejected` — missing / invalid / expired / kill-switched token, or signature mismatch. **Zero wire frames.**
- `DeviceNotFoundError` — unknown `device_id`.
- `CatalogNotFoundError` — missing catalog.
- `LookupError` — catalog entry missing `reboot` or `rebootIfRequired` OID names.
- `NetworkUnreachableError` / `SnmpTimeoutError` — wire failure on the `rebootIfRequired` GET (helper default-fires to fail-closed: `reason='vote_unreadable_fail_closed'`).
