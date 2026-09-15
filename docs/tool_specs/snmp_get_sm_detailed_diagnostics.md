---
name: snmp_get_sm_detailed_diagnostics
description: Read typed per-LUID diagnostics (jitter, CINR, Rx/Tx levels, retransmits) for one SM under a PMP 450i AP.
tier: 0
---

# snmp_get_sm_detailed_diagnostics

## Tier

**Tier 0** — Passive Telemetry (Read-Only). One wire GET per diagnostics OID name (jitter, CINR, Rx/Tx levels, retransmits, interface error counters — the reserved fields).

## Operational impact

Pure read against one SM under the AP. No write frames. Idempotent.

## Prerequisites

- `device_id` must resolve in `Inventory`.
- `luid` identifies the SM within the AP sector managed by `device_id`.
- Catalog entry for `(vendor, model, firmware)` must include the SM-diagnostics OID branch.

## Inputs

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `device_id` | `string` | yes | Inventory device id (the AP). |
| `luid` | `string` | yes | Per-LUID identifier of the target SM. |

## Outputs

`SmDetailedDiagnostics` carrying jitter, CINR, Rx/Tx levels, retransmits, and reserved interface error counters for one SM.

## Failure modes

- `DeviceNotFoundError` — unknown `device_id`.
- `CatalogNotFoundError` — missing catalog.
- `NetworkUnreachableError` / `SnmpTimeoutError` — wire failure.
