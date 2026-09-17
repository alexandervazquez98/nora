---
name: snmp_get_ap_summary
description: Read a typed AP summary (firmware, carrier, channel width, tx power, subscriber count, uptime) for a PMP 450i access point.
tier: 0
---

# snmp_get_ap_summary

## Tier

**Tier 0** — Passive Telemetry (Read-Only). Zero impact on subscriber traffic; non-disruptive SNMP GET against an inventory-known access point.

## Operational impact

Pure read of the catalog-resolved AP summary OIDs. No write frames, no SM-side effect, no maintenance-window dependency.

## Prerequisites

- `device_id` must resolve in `Inventory` (YAML-backed).
- The catalog entry for `(vendor, model, firmware)` must include the AP summary OID branch.

## Inputs

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `device_id` | `string` | yes | Inventory device id (e.g. `ap-7400-01`). |

## Outputs

Typed `ApSummary` carrying firmware, carrier frequency, channel width, tx power, subscriber count, and sys uptime. JSON-serialised through `model_dump(mode="json")`.

## Failure modes

- `DeviceNotFoundError` — unknown `device_id`.
- `CatalogNotFoundError` — no catalog for the device's `(vendor, model, firmware)` triple.
- `NetworkUnreachableError` / `SnmpTimeoutError` — wire failure (typed errors).
