---
name: snmp_run_spectrum_analysis
description: Run a typed spectrum sweep against a PMP 450i AP (ranked clean frequencies + noise-floor map). Tier-1 server-side gate requires operator_confirmed=True BEFORE any wire frame.
tier: 1
requires_operator_confirmed: true
---

# snmp_run_spectrum_analysis

## Tier

**Tier 1** — Potentially Disruptive / Active Telemetry. The sweep emits active RF probing — on the real radio this drops sector transmission and disassociates subscriber modules. The server-side gate refuses any call that arrives without `operator_confirmed=True` (the default, including the absent-parameter case).

## Operational impact

The sweep walks the three spectrum-noise-floor OIDs and folds the response into a typed `SpectrumAnalysis` carrying `ranked_clean_frequencies` (sorted by ascending noise floor), `noise_floor_dbm`, and `scan_started_at`. The Tier-1 gate fires BEFORE the maintenance-window check (highest-priority invariant), so an autonomous call without clearance never produces a wire frame.

## Prerequisites

- `device_id` must resolve in `Inventory`.
- The catalog entry for `(vendor, model, firmware)` must include the spectrum-noise-floor OID branch (`spectrumNoiseFloorA` / `B` / `C`).
- `Settings.nora_maintenance_window_minutes > 0` AND `now` inside the configured window.

## Operator-clearance gate (ADR-4 cross-validator invariant)

- `operator_confirmed: bool = False` (the default) — the tool raises `Tier1ClearanceRequired` BEFORE any SNMP GET. **Zero wire frames emitted.** The LLM orchestrator MUST request operator clearance before invoking.
- `operator_confirmed: bool = True` — the existing maintenance-window + spectrum-sweep logic proceeds unchanged. A confirmed clearance does NOT bypass the window check.

## Inputs

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `device_id` | `string` | yes | Inventory device id. |
| `operator_confirmed` | `bool` | no (default `False`) | Operator clearance gate. MUST be `True` for the sweep to proceed. |

## Outputs

`SpectrumAnalysis` carrying `ranked_clean_frequencies` (kHz), `noise_floor_dbm` (freq-kHz → noise-dBm map), and `scan_started_at`.

## Failure modes

- `Tier1ClearanceRequired` — `operator_confirmed` is `False` (or absent). Zero wire frames.
- `MaintenanceWindowViolation` — `now` falls outside the configured maintenance window.
- `DeviceNotFoundError` — unknown `device_id`.
- `CatalogNotFoundError` — missing catalog.
- `NetworkUnreachableError` / `SnmpTimeoutError` — wire failure.
