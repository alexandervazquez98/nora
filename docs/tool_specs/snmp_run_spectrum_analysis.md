---
name: snmp_run_spectrum_analysis
description: Run a Cambium PMP 450i spectrum sweep (real SET/GET-poll protocol) followed by a post-sweep HTTP fetch of `SpectrumAnalysis.xml` for ranked clean frequencies + noise-floor map. Tier-1 server-side gate requires operator_confirmed=True BEFORE any wire frame.
tier: 1
requires_operator_confirmed: true
---

# snmp_run_spectrum_analysis

## Tier

**Tier 1** — Potentially Disruptive / Active Telemetry. The sweep emits active RF probing — on the real radio this drops sector transmission and disassociates subscriber modules. The server-side gate refuses any call that arrives without `operator_confirmed=True` (the default, including the absent-parameter case).

## Operational impact

Two-phase operation against a Cambium PMP 450i (firmware 25.0.1 / 25.1):

1. **SNMP sweep (issue #62 / WU-3)** — SET `whispBoxSpectrumScanDuration` (`.220.0`) → SET `whispBoxSpectrumScanAction` (`.221.0`) to `8` (arm) then `1` (start) → GET-poll `.221.0` every `nora_spectrum_sweep_poll_interval_seconds` (default 1.0s) until the radio reports a completion sentinel `{0, 3, 4}` or `nora_spectrum_sweep_timeout_seconds` (default 150s) elapses.
2. **Post-sweep HTTP ladder (issue #70 / WU-2)** — only on `final_status == 4` (`idleCompleteSpectrumAnalysis`): GET `http://{host}/SpectrumAnalysis.xml` with bounded retry, optionally followed by a per-SM ladder. Failures are non-fatal — captured in `post_sweep_error`.

The Tier-1 gate fires BEFORE the maintenance-window check (highest-priority invariant), so an autonomous call without clearance never produces a wire frame. Operator-observed AP timing: ~95-105s sweep + ~15s SM re-association + ~5-15s per SM HTTP fetch.

## Prerequisites

- `device_id` must resolve in `Inventory`.
- The catalog entry for `(vendor, model, firmware)` must include `spectrumScanDuration` (`.220.0`) and `spectrumScanAction` (`.221.0`).
- `Settings.nora_maintenance_window_minutes > 0` AND `now` inside the configured window (else the sweep refuses before any wire frame).
- For `final_status == 4`, the radio's web server must be reachable on plain HTTP port 80 at `/SpectrumAnalysis.xml`. The fetch ladder tolerates ~15s of HTTP silence via bounded retry (`nora_spectrum_http_max_retries=5` × `nora_spectrum_http_retry_delay_seconds=3.0`).

## Operator-clearance gate (ADR-4 cross-validator invariant)

- `operator_confirmed: bool = False` (the default) — the tool raises `Tier1ClearanceRequired` BEFORE any SNMP GET. **Zero wire frames emitted.** The LLM orchestrator MUST request operator clearance before invoking.
- `operator_confirmed: bool = True` — the existing maintenance-window + sweep + HTTP ladder proceeds. A confirmed clearance does NOT bypass the window check.

## Inputs

| Name | Type | Required | Default | Bounds | Description |
|------|------|----------|---------|--------|-------------|
| `device_id` | `string` | yes | — | non-empty | Inventory device id. |
| `operator_confirmed` | `bool` | no | `False` | — | Operator clearance gate. MUST be `True` for the sweep to proceed. Default `False` is fail-closed. |
| `sweep_duration_seconds` | `int \| None` | no | `None` (→ Settings default 30) | `[1, 600]` (validator-enforced at boot AND at the Pydantic boundary) | SET value on `.220.0` and the scaled poll-loop guard. AP sweeps take ~95-105s regardless of this parameter; SM sweeps scale linearly. |

## Outputs

A `SpectrumSweepResult` model (JSON-serialised via `model_dump(mode="json")`):

```json
{
  "device_id": "192.0.2.10",
  "scan_started_at": "2026-09-19T12:34:56.789012+00:00",
  "scan_completed_at": "2026-09-19T12:36:31.012345+00:00",
  "sweep_duration_seconds": 30,
  "final_status": 4,
  "scan_outcome": "COMPLETED",
  "ranked_clean_frequencies": [3560.0, 3550.0, 3540.0, 3600.0, 3700.0, 3500.0, 3620.0, 3650.0],
  "noise_floor_dbm": {
    "3500.0": -65.0,
    "3540.0": -73.0,
    "3550.0": -79.0,
    "3560.0": -82.0,
    "3600.0": -70.0,
    "3620.0": -65.0,
    "3650.0": -57.0,
    "3700.0": -70.0
  },
  "post_sweep_error": ""
}
```

Field semantics:

- `device_id` — inventory device the sweep ran against (IPv4 string; sanitised at the MCP boundary per Zero-Leakage).
- `scan_started_at` / `scan_completed_at` — UTC ISO-8601 timestamps marking the SET-arm call and the first completion-sentinel poll, respectively.
- `sweep_duration_seconds` — echoes the duration SET on `.220.0` (tool kwarg override OR Settings default; the audit trail is self-contained).
- `final_status` — last polled value on `.221.0` (typically `4` on real-hardware COMPLETED; `0` defensive abort; `3` idle no-results; `-1` if the wire never returned).
- `scan_outcome` — `COMPLETED` | `TIMEOUT` | `ABORTED`.
- `ranked_clean_frequencies` — `list[float]` of MHz values, ascending by worst-leg `avg_dbm` (most negative = cleanest first), top `nora_spectrum_ranking_top_n` entries. Empty on `final_status in {0, 3}` or when the post-sweep ladder failed.
- `noise_floor_dbm` — `dict[str, float]` keyed by `"{freq_mhz:.1f}"`, value `float(worst_leg_avg_dbm)`. Empty on `final_status in {0, 3}` or when the post-sweep ladder failed.
- `post_sweep_error` — empty string on a clean sweep; one-line diagnostic on a sweep that completed with `final_status == 4` but the post-sweep HTTP fetch + XML parse ladder failed non-fatally (network unreachable, XML malformed, or one or more SM hosts failed). The sweep outcome stays `COMPLETED` — only the bin decode could not be performed.

## Algorithm: post-sweep HTTP ladder

Runs ONLY when `final_status == 4` (the success sentinel that implies results are available). Sentinel `0` (defensive abort) and `3` (idle, no results) skip the ladder; only `4` triggers the fetch.

1. **AP XML** — `fetch_spectrum_xml(ap_host, timeout_seconds=nora_spectrum_http_timeout_seconds=10.0, max_retries=5, retry_delay_seconds=3.0)`.
2. **SM re-association sleep** — if a future slice passes `sm_hosts` to the helper, sleep `nora_spectrum_sm_reassociation_timeout_seconds=15.0` (operator's production baseline; typical re-association observed: 8-15s).
3. **SM ladder** — sequentially `fetch_spectrum_xml(sm_host)` for each entry in `sm_hosts`. Each SM failure is captured in `post_sweep_error` but does NOT abort the helper — AP data is preserved.
4. **Parse + aggregate** — every XML payload is parsed via `parse_spectrum_xml` (stdlib `xml.etree.ElementTree`); bins are concatenated across AP + SMs.
5. **Aggregate** — `noise_floor_per_channel(bins)` computes per-frequency worst-leg `avg_dbm`; `rank_clean_frequencies(bins, top_n=nora_spectrum_ranking_top_n=10)` returns the top-N cleanest frequencies.

## Failure modes

- `Tier1ClearanceRequired` — `operator_confirmed` is `False` (or absent). Zero wire frames.
- `MaintenanceWindowViolation` — `now` falls outside the configured maintenance window.
- `DeviceNotFoundError` — unknown `device_id`.
- `CatalogNotFoundError` / `LookupError` — missing OID in the catalog (specifically `spectrumScanDuration` or `spectrumScanAction`).
- `NetworkUnreachableError` / `SnmpTimeoutError` — wire failure during the SNMP sweep or post-sweep HTTP ladder.
- `SpectrumSweepTimeout` — `.221.0` did not return a completion sentinel within `nora_spectrum_sweep_timeout_seconds`. Carries `device_id`, `duration_seconds`, `last_status`.
- `SpectrumHttpFetchError` / `SpectrumXmlParseError` — re-exported via `nora.drivers.exceptions`; on the post-sweep ladder these are NON-FATAL (folded into `post_sweep_error`).
- `ValueError` — `sweep_duration_seconds` outside `[1, 600]`; or a Settings misconfiguration (caught at boot by `_validate_spectrum_http_settings` for the 5 new knobs).

## Settings knobs (operator-tunable; defaults match operator's production baseline)

| Knob | Default | Bound | Purpose |
|------|---------|-------|---------|
| `nora_spectrum_sweep_duration_seconds` | 30 | `[1, 600]` | `.220.0` SET value (tool kwarg `sweep_duration_seconds` overrides). |
| `nora_spectrum_sweep_poll_interval_seconds` | 1.0 | — | GET-poll cadence on `.221.0`. |
| `nora_spectrum_sweep_timeout_seconds` | 150 | — | Poll-loop deadline. 150s covers AP (~95-105s) + SM (~15s + re-association) with headroom. |
| `nora_spectrum_http_timeout_seconds` | 10.0 | `[1.0, 60.0]` | Per-attempt HTTP timeout for `SpectrumAnalysis.xml` GET. |
| `nora_spectrum_http_max_retries` | 5 | `[0, 20]` | Retry attempts AFTER the initial GET (0 disables retry). |
| `nora_spectrum_http_retry_delay_seconds` | 3.0 | `[0.1, 30.0]` | Sleep between HTTP attempts. |
| `nora_spectrum_sm_reassociation_timeout_seconds` | 15.0 | `[1.0, 60.0]` | Wait between AP completion and SM XML fetch. |
| `nora_spectrum_ranking_top_n` | 10 | `[1, 100]` | Top-N for `ranked_clean_frequencies`. |

## Notes

- The post-sweep ladder is **sequential** for v1; concurrent SM fan-out is tracked as a follow-up.
- Zero-Leakage: every host literal in the result is the inventory's IPv4 literal (already TEST-NET-1 / RFC 5737 sanitised at the inventory layer); no new identifiers are embedded by the helper.
- The driver-layer air-gap permit for `httpx` is scoped to `src/nora/drivers/snmp_pmp450i/spectrum_http.py` ONLY (see `tests/test_driver_airgap.py::_AIRGAP_EXCEPTIONS`); every other driver / prompt module remains HTTP-free.
- Tier-1 contract: `operator_confirmed` defaults to `False` (fail-closed); the LLM orchestrator MUST request operator clearance before invoking.
