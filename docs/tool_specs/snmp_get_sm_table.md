---
name: snmp_get_sm_table
description: Read the typed subscriber baseline (ONLINE_ACTIVE / ACTIVE_DEGRADED / PRE_EXISTING_OFFLINE) for a PMP 450i AP.
tier: 0
---

# snmp_get_sm_table

## Tier

**Tier 0** — Passive Telemetry (Read-Only). The PRE_DIAGNOSTIC cross-check reads on-disk intervention history; no write frames are emitted.

## Operational impact

Walks the SM table over SNMP and folds the response into a typed `SubscriberSummary`. The cross-check ordering rule (intervention-history read BEFORE subscriber categorisation) is enforced inside the helper.

## Prerequisites

- `device_id` must resolve in `Inventory`.
- Catalog entry for `(vendor, model, firmware)` must include the SM-table OID branch.

## Inputs

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `device_id` | `string` | yes | Inventory device id. |

## Outputs

`SubscriberSummary` carrying three buckets (`ONLINE_ACTIVE`, `ACTIVE_DEGRADED`, `PRE_EXISTING_OFFLINE`) plus `baseline_size`.

## Failure modes

- `DeviceNotFoundError` — unknown `device_id`.
- `CatalogNotFoundError` — missing catalog.
- `NetworkUnreachableError` / `SnmpTimeoutError` — wire failure.
