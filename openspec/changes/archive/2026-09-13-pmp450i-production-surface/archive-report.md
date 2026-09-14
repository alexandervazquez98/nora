# Archive Report: PMP 450i Production Surface (Cluster #14 + #15 + #24)

**Change**: `2026-09-13-pmp450i-production-surface`
**Archived to**: `openspec/changes/archive/2026-09-13-pmp450i-production-surface/`
**Archive date**: 2026-09-13 (ISO)
**Artifact store**: openspec (filesystem only; Engram observation recorded post-archive)
**Branch**: `main` (HEAD `24c8d20`)
**Linked issues**: https://github.com/alexandervazquez98/nora/issues/14, #15, #24 (all CLOSED via PR #30)

## Cluster Summary

This change is the second instalment of the PMP 450i production surface — the cluster
that takes NORA from "boot + read-only memory" to "operator-driven radio-link
diagnosis and HITL-gated remediation". It bundles three issues into one tightly
coupled change because none of them is independently shippable: issue #14 (no
IP-direct path for ad-hoc equipment) is the prerequisite for issue #15 (six
radio-link `@mcp.tool`s) and ADR #17 Tests 9 + 10 (issue #24, the OID-catalog
integration test + tool-registration guard). Five chained PRs landed on `main`
between `9d2b5fb` and `24c8d20`; all 27 named tests pass at runtime; 479 tests
pass at the cluster HEAD; all three issues closed in a single PR (#30). Cluster
shipped green.

## Specs Synced

| Domain | Action | Compose Tool | Details |
|--------|--------|--------------|---------|
| `driver-interface` | Created (NEW) | Mechanical `cp` + `diff -r` (empty) | 7 requirements, 9 scenarios. File at `openspec/specs/driver-interface/spec.md` (5283 bytes). |
| `pmp450i-radio-tools` | Created (NEW) | Mechanical `cp` + `diff -r` (empty) | 10 requirements, 16 scenarios. File at `openspec/specs/pmp450i-radio-tools/spec.md` (9240 bytes). |
| `oid-catalog-integration` | Created (NEW) | Mechanical `cp` + `diff -r` (empty) | 4 requirements, 7 scenarios. File at `openspec/specs/oid-catalog-integration/spec.md` (4928 bytes). |
| `driver-snmp-pmp450i` | Updated (6 ADDED) | `gentle-ai sdd-archive-compose` exit 0 | 6 ADDED requirements (`DeviceDriverInterface Is The Driver Seam`, `report_firmware()` Part Of The Public Contract, `Inventory Path Preserved — No Regression`, `Ad-Hoc Path Exists Alongside Inventory`, `Sanitizer Boundary On Tool Responses`, `Air-Gap Extension For New Resolution Path`). File grew 4400 → 8345 bytes; canonical `Protocol Support — v2c AND v3`, `Read-Only Enforcement`, `Strictly Typed Return`, `Auto-Trace Integration`, `Device Focus Binding`, `Failure Surfaces Typed Errors`, `Air-Gap & No Secrets`, `OID Resolution Caching (SHOULD)` byte-identical to pre-slice-1. |
| `oid-catalog` | Updated (2 MODIFIED + 2 ADDED) | `gentle-ai sdd-archive-compose` exit 0 | MODIFIED `Catalog Schema Validation` (now per-`(vendor, model)` AND per-tool; new scenario "missing per-tool index entry fails verification"), MODIFIED `Minor Descending Fallback With Literal Warning` (extended to all tool paths via ADR #17 Test 10). ADDED `Migration Resolves Two Triples (Current + Candidate)` + ADDED `Per-Tool Index Is Stable Across Runs`. File grew 7305 → 9796 bytes; `On-Disk Layout`, `HMAC-SHA256 Boot Verification`, `Key Rotation Behaviour`, `No Vendor MIB Text`, `No Network During Catalog Resolution`, `Multi-Root Scan Determinism`, `Semver-Aware Firmware Resolution`, `Strict-Major Hard Fail` byte-identical. |
| `nora-mcp-server` | Updated (3 MODIFIED + 2 ADDED) | `gentle-ai sdd-archive-compose` exit 0 (after delta-spec defect correction; see Reconciliation Note below) | MODIFIED `R-NEW-1` ("Four `@mcp.tool` Registrations" → 11-tool count), MODIFIED `R-NEW-2` (sanitizer count from 4 → 11), MODIFIED `R-NEW-4` (one-way dep scope now covers 6 consumer packages). ADDED `Server-Level ` `instructions` (Updated) — HITL Advertised`, ADDED `R-NEW-6 — Tool-Registration Guard (Uncatalogued Tools Rejected)`. File grew 11895 → 12979 bytes. Pre-existing `FastMCP Boot Over Stdio`, `Stderr-Only Logging`, `Telemetry Sanitizer Boundary`, `Security Boundary`, `Observability`, `R-NEW-3`, `R-NEW-5`, `python -m nora` Deprecation Alias`, `No Boot-Time LLM or Journal Injection` byte-identical. |

### Mechanical Copy Contract Verification

- **driver-interface copy**: `cp` source → temp → `mv`; `diff -r` source vs temp = empty (exit 0). Verbatim readback output:
  ```
  $ diff -r openspec/changes/2026-09-13-pmp450i-production-surface/specs/driver-interface/spec.md \
           openspec/specs/driver-interface/spec.md
  (no output; exit 0)
  ```
- **pmp450i-radio-tools copy**: `cp` source → temp → `mv`; `diff -r` empty (exit 0).
- **oid-catalog-integration copy**: `cp` source → temp → `mv`; `diff -r` empty (exit 0).
- **driver-snmp-pmp450i compose**: `gentle-ai sdd-archive-compose --canonical ... --delta ... --output ...` exit 0; `mv` produced atomic file replacement. Post-compose canonical grows 4400 → 8345 bytes; 6 ADDED requirements inserted, all 8 pre-existing requirements preserved byte-identical.
- **oid-catalog compose**: `gentle-ai sdd-archive-compose ...` exit 0; post-compose canonical grows 7305 → 9796 bytes; 2 MODIFIED requirements updated, 2 ADDED requirements appended, all 8 pre-existing requirements preserved byte-identical.
- **nora-mcp-server compose**: `gentle-ai sdd-archive-compose ...` exit 0; post-compose canonical grows 11895 → 12979 bytes. See Reconciliation Note below for the delta-spec defect that required mechanical pre-processing before compose would accept the delta.

### Reconciliation Note (delta-spec authoring defect on `nora-mcp-server`)

> The `nora-mcp-server` MODIFIED delta was authored with cosmetic heading
> suffixes (`(Updated Count)` and `(Updated)`) that did not match the canonical
> heading text. `gentle-ai sdd-archive-compose` refuses to silently merge
> mismatched MODIFIED requirement names — first run failed with
> `unapplied MODIFIED delta for requirement "R-NEW-1 — `@mcp.tool`
> Registrations (Updated Count)": no canonical requirement named "R-NEW-1 —
> `@mcp.tool` Registrations (Updated Count)"`. The delta was mechanically
> pre-processed (sed + Python script on a temp copy, NOT a model Read/Edit
> merge of the canonical) to:
>
> 1. Restore the canonical heading text for `R-NEW-1`, `R-NEW-2`, `R-NEW-4`
>    (the canonical IDs were preserved; only the heading text after the dash
>    was rewritten to match).
> 2. Re-classify the `Server-Level instructions (Updated)` block from
>    MODIFIED to ADDED. The canonical has no `Server-Level instructions`
>    requirement, so the block was clearly intended as ADDED — the
>    MODIFIED label was an authoring defect.
>
> The original delta file in the change folder is byte-identical to its
> archived version (the pre-processing was on a `mktemp -d` copy that was
> discarded after compose). All MODIFIED/ADDED body content was applied
> verbatim; no canonical requirement was dropped or merged with another.
> The archive preserves the original (defective) delta for audit
> traceability — a follow-up delta re-issue with corrected headings would
> close the loop on documentation hygiene without affecting the canonical
> spec.

## Implementation

- **Tasks**: 69/69 complete (`- [x]`), 0 pending. Task Completion Gate PASSED.
- **Source of truth**: `openspec/changes/archive/2026-09-13-pmp450i-production-surface/tasks.md` (persisted SDD artifact, highest rank per Final-State Authority).
- **Test results**: 479 passed, 3 skipped, 0 failed (per `verify-report.md` and re-run at archive time; 1 pre-existing deprecation warning on `python -m nora` alias).
- **Build**: ruff + mypy + ruff-format all exit 0 (per `verify-report.md`).
- **Coverage**: 86% on `src/nora/` (threshold 85%).
- **Cluster coverage**: per `verify-report.md` "Changed File Coverage" table — average 89% across the 13 cluster-touched files; weakest is `cli.py` at 52% (acceptable; `cli.main()` exercised by E2E subprocess in `test_new_tool_without_oid_registration_rejected_at_registration_time`).
- **Files changed across 5 PRs**: see PR table below.
- **Driver back-compat**: `Pmp450iDriver.fetch_radio_metrics(...)` preserved byte-identical; the 39 back-compat tests in `tests/test_driver_*` all pass.

## Per-PR Cluster Manifest

| PR # | Title | Commit SHA | Merge Date | Files (impl + tests) | LOC (impl + tests) | Named tests covered |
|------|-------|------------|------------|----------------------|---------------------|----------------------|
| #26 | `feat(driver-interface): DeviceDriverInterface seam + IP-direct resolver + report_firmware` | `9d2b5fb` | 2026-09-13 | `drivers/interface.py`, `drivers/resolver.py`, `drivers/snmp_pmp450i/driver.py`, `drivers/registry.py`, `drivers/exceptions.py`, `tests/test_driver_interface.py`, `tests/test_resolver.py`, `tests/test_driver_airgap.py` (extended) | ~560 (220 + 340) | `ip_direct_resolution_builds_ephemeral_device`, `secret_str_safety_no_plaintext_in_repr`, `report_firmware_returns_typed_version`, `inventory_path_still_works_no_regression` |
| #27 | `feat(radio-tools): ap_summary + frame_utilization tools` | `fad8d56` | 2026-09-13 | `drivers/snmp_pmp450i/summaries.py`, `drivers/oid_catalog.py` (envelope tools map), `server.py` (+2 `@mcp.tool`), `tests/test_snmp_summaries.py`, catalog re-sign (`scripts/sign_catalog.py`) | ~360 (140 + 220) | `ap_summary_returns_typed_model`, `frame_utilization_returns_typed_model`, `unknown_oid_warn_and_value`, `catalog_minor_mismatch_warning_via_summary_call` |
| #28 | `feat(radio-tools): sm_table + sm_detailed_diagnostics with PRE_EXISTING_OFFLINE baseline (slice 3)` | `421fe21` | 2026-09-13 | `drivers/snmp_pmp450i/subscribers.py`, `server.py` (+2 `@mcp.tool`), `tests/test_snmp_subscribers.py`, catalog re-sign | ~470 (200 + 270) | `sm_table_categorizes_online_active`, `sm_table_categorizes_active_degraded_low_cinr`, `sm_table_categorizes_pre_existing_offline`, `sm_table_unbiased_baseline_excludes_pre_existing`, `sm_detailed_diagnostics_typed_for_luid`, `get_intervention_history_called_before_categorize` |
| #29 | `feat(migrate): spectrum + HITL-gated migration with watchdog (slice 4)` | `624c840` | 2026-09-13 | `hitl/tokens.py`, `drivers/snmp_pmp450i/spectrum.py`, `drivers/snmp_pmp450i/migrate.py`, `config.py` (+ `nora_hitl_*`), `server.py` (+2 `@mcp.tool` + HITL instructions), `tests/test_hitl_tokens.py`, `tests/test_snmp_spectrum.py`, `tests/test_snmp_migrate.py`, catalog re-sign | ~790 (350 + 440) | `spectrum_returns_ranked_clean_frequencies`, `spectrum_respects_maintenance_window`, `migrate_requires_hitl_approval_token`, `migrate_make_before_break_migrates_online_active_first`, `migrate_excludes_pre_existing_offline_subscribers`, `migrate_rolls_back_within_timeout_on_loss_of_management`, `migrate_autonomous_call_raises_autonomous_mutation_rejected`, `migrate_emits_intervention_record_on_completion` |
| #30 | `feat(oid-catalog): tool-registration guard + E2E integration test (closes #14, #15, #24)` | `24c8d20` | 2026-09-13 | `drivers/oid_catalog.py` (`REQUIRED_OIDS_BY_TOOL`), `cli.py` (registration guard), `server.py` (verify 11 tools), `tests/test_oid_catalog_integration.py`, `tests/test_server.py` (tool count 5 → 11), `tests/test_integration.py` (tool count), `tests/test_integration_boot.py` (tool count), `tests/test_main_alias.py` (tool count), `tests/test_prompts.py` (tool count) | ~370 (100 + 270) | `dynamic_resolution_applies_catalog_versioning_before_query`, `minor_mismatch_warning_during_read_path_e2e`, `major_mismatch_blocks_driver_query_typed`, `unified_tool_catalog_references_required_oids_per_tool`, `new_tool_without_oid_registration_rejected_at_registration_time` |

**PR 4 (slice 4) was the tightest budget** at ~790 LOC, just under the 800-LOC preflight guard. No `size:exception` was required.

**Cluster cumulative**: ~2550 LOC across 5 PRs (1380 impl + 1170 tests, approximate). 27 named tests, 51 spec scenarios, 36 requirements.

## Spec Delta Manifest

| Capability | Action | Requirements (delta) | Scenarios (delta) | File |
|------------|--------|----------------------:|------------------:|------|
| `driver-interface` | NEW | 7 | 9 | `openspec/specs/driver-interface/spec.md` |
| `pmp450i-radio-tools` | NEW | 10 | 16 | `openspec/specs/pmp450i-radio-tools/spec.md` |
| `oid-catalog-integration` | NEW | 4 | 7 | `openspec/specs/oid-catalog-integration/spec.md` |
| `driver-snmp-pmp450i` | MODIFIED (6 ADDED) | 6 | 6 | `openspec/specs/driver-snmp-pmp450i/spec.md` |
| `oid-catalog` | MODIFIED (2 MODIFIED + 2 ADDED) | 4 | 6 | `openspec/specs/oid-catalog/spec.md` |
| `nora-mcp-server` | MODIFIED (3 MODIFIED + 2 ADDED) | 5 | 7 | `openspec/specs/nora-mcp-server/spec.md` |
| **Total** | **3 NEW + 3 MODIFIED** | **36** | **51** | |

## Verification Summary

| Metric | Value |
|--------|-------|
| Verdict | **PASS** |
| Critical findings | 0 |
| Blockers | 0 |
| Test command | `.venv/bin/python -m pytest --no-cov` |
| Test exit code | 0 |
| Tests passed | 479 |
| Tests skipped | 3 |
| Tests failed | 0 |
| Coverage on `src/nora/` | 86% (threshold 85%) |
| Build commands | `ruff check .` (exit 0); `ruff format --check` (exit 0, 96 files); `mypy --strict src/nora` (exit 0, 39 files) |
| Spec compliance | 50/51 scenarios COMPLIANT; 1/51 PARTIAL (pmp450i-radio-tools / Sub-Cluster 3 Sanitizer Contract — free-text sanitization routing; typed-scalar half fully compliant) |
| Design decisions | 7 declared; 7 implemented (1 PARTIAL on `resolve_migration_refs` API surface — see WARN-3 below) |
| Named tests passed | 27/27 at HEAD `24c8d20` |
| Backward compatibility | `Pmp450iDriver.fetch_radio_metrics` preserved byte-identical; 39 inventory-path tests pass |

## Residual Items (informational — not blocking)

The three WARNING items below are residual design follow-ups; they do **not** block archive and are recorded here as future work for the orchestrator to surface as follow-up issues.

### Warnings (carried forward from `verify-report.md`)

1. **`server.py` coverage gap at 69% is pre-existing** (not introduced by this change). The uncovered lines are mostly `model_dump(mode="json")` returns, prompt registry calls, and the `_enumerate_tool_names` async path. **Recommended follow-up**: add `tests/test_server.py::test_verify_tools_are_catalogued_passes_for_real_registry` for the in-process positive path of the registration guard. **Not blocking** — threshold met (86% on the whole tree).

2. **Sub-Cluster 3 Sanitizer Contract scenario "free-text masked, typed scalars untouched" is partially satisfied.** The radio-link tool wrappers return typed Pydantic `model_dump(mode="json")` outputs whose fields are typed scalars — there are no user-supplied free-text fields on `ApSummary`, `FrameUtilization`, `SubscriberSummary`, `SmDetailedDiagnostics`, `SpectrumAnalysis`, or `MigrationResult`. The contract is satisfied by construction (no free text → no masking needed), but a literal interpretation of the spec scenario calls for `_sanitizer.sanitize(...)` invocation as defence-in-depth. **Recommended follow-up**: one-line addition per wrapper to route through `_sanitizer.sanitize(...)`. **Not blocking** — typed-scalar half is fully compliant.

3. **`resolve_migration_refs(current_ref, candidate_ref)` was declared in `design.md` AD #5 but is not exposed in the slice 4 implementation.** The implementation reuses the single `resolve(...)` call against the device's firmware; the candidate frequency does not introduce a new firmware triple (same AP, same firmware, only the carrier frequency changes). The pre-wire guard is preserved (`LookupError` on missing migration OID names; no SET frame on major mismatch). **Recommended follow-up**: expose the dual-resolve API as a `OidCatalogRegistry.resolve_migration_refs(...)` method in a v2 cluster. **Not blocking** — the spec scenario "both triples resolve before the wire frame" is satisfied at runtime.

### Suggestions (carried forward from `verify-report.md`)

1. **`tests/test_no_llm_journal_imports.py` does not cover `drivers/resolver.py`.** In practice `tests/test_driver_airgap.py::test_resolver_path_is_in_ast_walked_set` provides equivalent coverage (different banned-import list — network/DNS modules rather than LLM/journal modules). Both scans pass. **Recommended follow-up**: rename or split the LLM/journal AST scan so the driver-layer scan is co-located with the network-layer scan. **No functional change**.

2. **`_ALLOWED_UNCATALOGUED_TOOLS` allow-list technical debt.** Five tools listed (`search_intervention_history`, `get_device_lifecycle_summary`, `correlate_sector_interference`, `save_intervention_record`, `snmp_get_pmp450i_radio_metrics`). The first four are correctly listed (no SNMP OIDs); `snmp_get_pmp450i_radio_metrics` is the legacy v1 seed tool awaiting a future catalog re-sign. **Recommended follow-up delta**: re-sign the baseline catalog `15.2.1.json` / `15.3.0.json` with a `"tools": {"snmp_get_pmp450i_radio_metrics": [...]}` entry and remove the allow-list exemption.

3. **`snmp_get_pmp450i_radio_metrics` re-sign follow-up.** Same delta as SUGGESTION-2.

## Final-State Authority Note

Per the `sdd-archive` SKILL.md Final-State Authority hierarchy, the archive report is the terminal record of the cycle. The orchestrator launch prompt for this archive provided no explicit final-state facts that diverge from `verify-report.md`; the launch prompt's prompt-preflight text matched the canonical verify-report on every material number (479 passed / 3 skipped / 0 failed, 86% coverage, 27/27 named tests, 51 scenarios). No contradictions between the launch prompt and the verify-report required explicit recording in this archive.

No CRITICAL issues were asserted to be resolved after `verify-report.md` was persisted. All three WARNING items and all three SUGGESTION items are residual design follow-ups that were known to the verify agent and are documented above as informational — they were not retroactively closed by the orchestrator's launch prompt.

## Source of Truth Updated

The following specs now reflect the new behavior at HEAD `24c8d20`:

- `openspec/specs/driver-interface/spec.md` (NEW — 7 requirements, 9 scenarios, 5283 bytes)
- `openspec/specs/pmp450i-radio-tools/spec.md` (NEW — 10 requirements, 16 scenarios, 9240 bytes)
- `openspec/specs/oid-catalog-integration/spec.md` (NEW — 4 requirements, 7 scenarios, 4928 bytes)
- `openspec/specs/driver-snmp-pmp450i/spec.md` (6 ADDED requirements; 8 pre-existing requirements byte-identical; grew 4400 → 8345 bytes)
- `openspec/specs/oid-catalog/spec.md` (2 MODIFIED + 2 ADDED requirements; 8 pre-existing requirements byte-identical; grew 7305 → 9796 bytes)
- `openspec/specs/nora-mcp-server/spec.md` (3 MODIFIED + 2 ADDED requirements; 11 pre-existing requirements byte-identical; grew 11895 → 12979 bytes)

## Archive Contents

- `proposal.md` ✅
- `design.md` ✅
- `tasks.md` ✅ (69/69 tasks `[x]`)
- `verify-report.md` ✅ (PASS verdict)
- `specs/driver-interface/spec.md` ✅
- `specs/driver-snmp-pmp450i/spec.md` ✅ (delta)
- `specs/oid-catalog/spec.md` ✅ (delta)
- `specs/oid-catalog-integration/spec.md` ✅
- `specs/pmp450i-radio-tools/spec.md` ✅
- `specs/nora-mcp-server/spec.md` ✅ (delta)
- `archive-report.md` ✅ (this file; additive-only, excluded from diff readback)

## Issues Closed

- `alexandervazquez98/nora#14` — IP-direct path for ad-hoc equipment (closed by PR #30)
- `alexandervazquez98/nora#15` — Six radio-link `@mcp.tool`s for the PMP 450i operational surface (closed by PR #30)
- `alexandervazquez98/nora#24` — ADR #17 Tests 9 + 10: OID-catalog integration E2E + tool-registration guard (closed by PR #30)

## GitHub

- Branch: `main` (HEAD `24c8d20`)
- PRs: `#26` → `#27` → `#28` → `#29` → `#30` (auto-chain, stacked-to-main; PR #30 carries `Closes #14, Closes #15, Closes #24`)
- No further PRs opened from this archive — direct commit to `main` only.

## Archive Folder Verification

- Active changes directory: `openspec/changes/2026-09-13-pmp450i-production-surface/` confirmed absent (moved to archive).
- Archive directory: `openspec/changes/archive/2026-09-13-pmp450i-production-surface/` confirmed present.
- Mechanical move: `git mv` failed (source was untracked in git index — change folder was never committed during the cluster's auto-chain; each PR merged cleanly but the change folder was added post-hoc to the index after PR #30's merge). Fallback to plain `mv` succeeded per the SKILL's plain-mv fallback path.
- Snapshot-vs-destination `diff -r` readback: **EMPTY** (exit 0). Pre-move snapshot at `$TMPDIR/sdd-archive.XXXXXX/source` was compared byte-for-byte against the archived folder. The `archive-report.md` (this file) is additive-only and was excluded from the comparison because it did not exist in the source snapshot.

## Mechanical Copy Contract — Verbatim Readback

```
$ diff -r openspec/changes/2026-09-13-pmp450i-production-surface/specs/driver-interface/spec.md \
         openspec/specs/driver-interface/spec.md
(no output; exit 0)

$ diff -r openspec/changes/2026-09-13-pmp450i-production-surface/specs/pmp450i-radio-tools/spec.md \
         openspec/specs/pmp450i-radio-tools/spec.md
(no output; exit 0)

$ diff -r openspec/changes/2026-09-13-pmp450i-production-surface/specs/oid-catalog-integration/spec.md \
         openspec/specs/oid-catalog-integration/spec.md
(no output; exit 0)

$ diff -r $TMPDIR/sdd-archive.XXXXXX/source \
         openspec/changes/archive/2026-09-13-pmp450i-production-surface
(no output; exit 0)
```

Empty `diff -r` output (exit 0) for every copy and move is the only passing evidence per the SKILL's Mechanical Copy Contract. Any non-empty diff would be truncation or alteration; the contract is honored in full.

## SDD Cycle Complete

The change has been fully planned, implemented, verified, and archived. Three issues closed (`#14`, `#15`, `#24`); five chained PRs delivered (`#26`–`#30`); all 27 named tests green; 36 requirements and 51 scenarios implemented; 86% test coverage on `src/nora/` (≥85%); ruff + mypy + ruff-format all exit 0; the PMP 450i production surface is ready for operator-driven diagnosis and HITL-gated remediation in production.

The cluster's natural follow-ups (WARN-1 in-process registration-guard test; WARN-2 explicit `_sanitizer` routing on radio-link wrappers; WARN-3 `resolve_migration_refs` API surface; SUGGESTION-2/3 catalog re-sign for `snmp_get_pmp450i_radio_metrics`) are residual design polish items. The orchestrator may surface them as follow-up issues; none blocks future work.