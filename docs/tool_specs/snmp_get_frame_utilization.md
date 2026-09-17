---
name: snmp_get_frame_utilization
description: Read the typed downlink + uplink frame-utilization percentages for a PMP 450i AP.
tier: 0
---

# snmp_get_frame_utilization

## Tier

**Tier 0** — Passive Telemetry (Read-Only). Two SNMP GETs against catalog-resolved frame-utilization OIDs.

## Operational impact

Pure read. Idempotent across repeated invocations.

## Prerequisites

- `device_id` must resolve in `Inventory`.
- Catalog entry for `(vendor, model, firmware)` must include the frame-utilization OID branch.

## Inputs

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `device_id` | `string` | yes | Inventory device id. |

## Outputs

`FrameUtilization` carrying `frame_utilization_dl_pct` and `frame_utilization_ul_pct`.

## Failure modes

- `DeviceNotFoundError` — unknown `device_id`.
- `CatalogNotFoundError` — missing catalog.
- `NetworkUnreachableError` / `SnmpTimeoutError` — wire failure.
