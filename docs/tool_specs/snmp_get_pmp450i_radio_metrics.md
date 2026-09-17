---
name: snmp_get_pmp450i_radio_metrics
description: Read the typed `RadioMetricsReport` (downlink/uplink rate, signal strength, SSR, modulation) for a PMP 450i radio.
tier: 0
---

# snmp_get_pmp450i_radio_metrics

## Tier

**Tier 0** — Passive Telemetry (Read-Only). Six SNMP GETs against catalog-resolved radio-metrics OIDs; subsequent reads are idempotent.

## Operational impact

The canonical entry point for typed RF telemetry. The legacy `@mcp.tool` that predates the per-tool catalog envelope — kept on the registry via the `ALLOWED_UNCATALOGUED` allow-list until a follow-up re-signs the baseline.

## Prerequisites

- `device_id` must resolve in `Inventory`.
- Catalog entry for `(vendor, model, firmware)` must include the radio-metrics OID branch.

## Inputs

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `device_id` | `string` | yes | Inventory device id. |

## Outputs

`RadioMetricsReport` carrying `radio_downlink_rate`, `radio_uplink_rate`, `signal_strength_rx`, `signal_strength_tx`, `ssr`, `modulation_mode`.

## Failure modes

- `DeviceNotFoundError` — unknown `device_id`.
- `CatalogNotFoundError` — missing catalog.
- `NetworkUnreachableError` / `SnmpTimeoutError` — wire failure.
