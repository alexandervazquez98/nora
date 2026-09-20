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

`SubscriberSummary` carrying three buckets (`ONLINE_ACTIVE`, `ACTIVE_DEGRADED`, `PRE_EXISTING_OFFLINE`) plus `baseline_size`. Every `SubscriberRecord` row in every bucket carries two optional fields per issue #69:

| Field | Type | Source OID | Wire type | Notes |
|-------|------|------------|-----------|-------|
| `site_name` | `string` | `whispLinkEntry.33` (`linkSiteName`) | `DisplayString` | Free-text session identifier (e.g. `BAJ02-VVU-TIJU-017`). Sanitized at the MCP boundary per the standard Zero-Leakage `Sanitizer` policy. Defaults to `""` when the radio omits the column. |
| `ip_address` | `string` | `whispLinkEntry.69` (`linkIpAddress`) | `IpAddress` | Management IP in dotted-quad form (`"192.0.2.31"`). Typed scalar — bypasses the `Sanitizer` (RFC 5737 TEST-NET-1 expected at the inventory layer). The null IPv4 sentinel `"0.0.0.0"` is the heuristic trigger that routes the row to `PRE_EXISTING_OFFLINE`. |

## Failure modes

- `DeviceNotFoundError` — unknown `device_id`.
- `CatalogNotFoundError` — missing catalog.
- `NetworkUnreachableError` / `SnmpTimeoutError` — wire failure.

## Informational

- **`ip_address == "0.0.0.0"` routes to `PRE_EXISTING_OFFLINE`.** A row whose management IP is the null IPv4 sentinel is classified pre-existing offline regardless of `session_uptime` or `link_status`. The recovered-subscriber invariant (issue #58) is preserved: a row with `session_uptime > 0` AND `link_status == "inSession"` AND `ip_address != "0.0.0.0"` is categorised by signal health, never by IP alone.
