# Archive Report: 2026-09-12-oid-catalog-hybrid-semver

**Change**: `2026-09-12-oid-catalog-hybrid-semver`
**Archived on**: 2026-09-12 (ISO date used for archive folder prefix; archive folder shares the change's own date)
**Archived to**: `openspec/changes/archive/2026-09-12-oid-catalog-hybrid-semver/`
**Spec store**: `openspec` (filesystem-native)
**Outcome**: **success** — SDD cycle complete.

---

## Final State (what shipped)

The change is fully merged into `main` and archived. Two chained PRs landed against the `stacked-to-main` chain strategy; both passed strict-TDD, ruff check, mypy --strict, and ruff format --check on the merge commit.

| Field | Value |
|-------|-------|
| `main` HEAD at archive | `59bd4157b4f97c7dca505dad7fa4c904c8280cb5` |
| PR 1 commit (merged) | `055fb97` — `feat(oid-catalog): p1-hybrid-loading — multi-root scan + REQUIRED_OIDS table (closes #13)` |
| PR 2 commit (merged) | `59bd415` — `feat(oid-catalog): p2-semver-resolution — semver-aware resolve + literal fallback warning` |
| PR 1 issue | closes #13 (multi-root scan + per-(vendor,model) REQUIRED_OIDS) |
| PR 2 issue | continues ADR #17 P2 (semver resolution, strict-major, minor fallback) |
| Test count final | **298 passed**, 2 skipped (pre-existing timeout skips, unchanged from pre-change baseline), 0 failed |
| Toolchain (final) | `pytest` ✅ · `ruff check` ✅ · `mypy --strict src/nora` ✅ (27 files clean) · `ruff format --check` ✅ (62 files formatted) |
| CRITICAL issues | **0** (PR 1: 0; PR 2: 0) |
| WARNING issues | **0 open** (PR 1 raised 1 WARNING on `tasks.md` documentation drift; per orchestrator launch prompt the WARNING is closed) |
| SUGGESTION issues | **2** (PR 2: multi-minor regression test not in suite, `+build` metadata path untested) — non-blocking, no spec change required |

### Per-PR outcome

| PR | Branch | Outcome | Source of truth |
|----|--------|---------|-----------------|
| PR 1 (`p1-hybrid-loading`) | `feat/p1-hybrid-loading` | PASS WITH WARNINGS (now closed) | `verify-report.md` — 294 passed, 2 skipped; WARNING-1 was documentation drift (`tasks.md` affected-files column omitting four integration test files that received a necessary `BUILTIN_BASELINE_SIGNING_KEY` swap) |
| PR 2 (`p2-semver-resolution`) | `feat/p2-semver-resolution` | PASS | `verify-report-2.md` — 298 passed, 2 skipped; 0 CRITICAL, 0 WARNING, 2 SUGGESTIONS; locking fixture reissued; operator-only path preserved (Pass 1 short-circuit at `oid_catalog.py:142-143`) |

---

## Specs Synced

`gentle-ai sdd-archive-compose` (gentle-ai.sdd-archive-compose/v1) was the only merge mechanism. Zero manual Read/Edit composition. Each delta was extracted to a per-domain file under the change's `specs/` tree and atomically composed into the canonical spec via `sdd-archive-compose`; the compose tool's zero exit is the sole composition evidence per the archive protocol.

| Domain | Canonical file | Delta applied | Action | Requirements delta |
|--------|----------------|---------------|--------|--------------------|
| `oid-catalog` | `openspec/specs/oid-catalog/spec.md` | `openspec/changes/.../specs/oid-catalog/spec.md` (per-domain extract) | Updated | **4 ADDED, 2 MODIFIED, 1 REMOVED** |
| `driver-snmp-pmp450i` | `openspec/specs/driver-snmp-pmp450i/spec.md` | n/a (wording-only note in single-file delta) | No change | **0** — delta noted "Wording only: the driver consumes the catalog returned by `OidCatalogRegistry.resolve(...)`, including minor-mismatch fallbacks"; existing Cross-References entry (`consumes the verified catalog at fetch time`) already covers the dependency. No formal delta operations specified. |
| `prompt-registry` | `openspec/specs/prompt-registry/spec.md` | n/a (cross-reference-only note in single-file delta) | No change | **0** — delta noted "Cross-reference only: oid-catalog mirrors the same layered-load pattern"; no formal ADDED Requirement specified. Existing Cross-References already name `driver-snmp-pmp450i` and `telemetry-sanitizer`; the prompt-registry layered-load pattern is unchanged. |

### `oid-catalog` spec — requirement-level delta

**MODIFIED Requirements (2):**

| Requirement | Reason |
|-------------|--------|
| `On-Disk Layout` | Now spans two roots (built-in via `importlib.resources` + operator override via `Settings.oid_catalogs_path`); override wins on `(vendor, model, firmware)` conflict. |
| `Catalog Schema Validation` | Required-OID set scoped per `(vendor, model)` (was global `REQUIRED_OIDS` module-level frozenset). |

**REMOVED Requirements (1):**

| Requirement | Reason / Migration |
|-------------|---------------------|
| `Per-Firmware Pin` | Reason: Exact-pin-only blocks #14/#15 multi-vendor. Migration: `test_unknown_firmware_raises_catalog_not_found` is reissued under `Strict-Major Hard Fail` — fixture requests `99.0.0` against only `15.x` catalogs and still raises a typed `CatalogNotFoundError`. |

**ADDED Requirements (4):**

| Requirement | Source acceptance criterion |
|-------------|----------------------------|
| `Multi-Root Scan Determinism` | ADR #17 P1: "Override gana sobre built-in" / "Catálogo sin firma válida = fail-fast" / "Precedencia determinista sin race conditions" |
| `Semver-Aware Firmware Resolution` | ADR #17 P2: "Firmware idéntico al catálogo: sin warning" |
| `Strict-Major Hard Fail` | ADR #17 P2: "Major mismatch: hard fail con excepción tipada nombrando el major mismatch" |
| `Minor Descending Fallback With Literal Warning` | ADR #17 P2: "Minor mismatch ... fallback al más cercano + warning literal" |

**Unchanged requirements preserved (4):** `HMAC-SHA256 Boot Verification`, `Key Rotation Behaviour`, `No Vendor MIB Text`, `No Network During Catalog Resolution`. Cross-References section preserved byte-identical (3 entries: `secure-configuration`, `driver-snmp-pmp450i`, `session-journal`).

The final `oid-catalog/spec.md` contains **10 requirements** total (was 7, -1 removed +4 added).

### Compose invocation evidence

```
gentle-ai sdd-archive-compose \
  --canonical "openspec/specs/oid-catalog/spec.md" \
  --delta "openspec/changes/2026-09-12-oid-catalog-hybrid-semver/specs/oid-catalog/spec.md" \
  --output "openspec/specs/oid-catalog/spec.md.compose-tmp"
EXIT: 0
```

The canonical was then atomically replaced via `mv spec.md.compose-tmp spec.md` (zero exit).

---

## Archive Contents

```
openspec/changes/archive/2026-09-12-oid-catalog-hybrid-semver/
├── archive-report.md           ← this file (additive; did not exist in source change folder)
├── design.md                   (6,976 B) — PR slice plan, decision table, risk mitigation
├── proposal.md                 (4,048 B) — intent, scope, approach, rollback
├── spec.md                     (6,192 B) — single-file delta (3 capabilities: oid-catalog + wording-only + cross-ref-only notes)
├── specs/                      ← per-domain delta extracted for the compose step (audit trail of what was composed)
│   └── oid-catalog/
│       └── spec.md
├── tasks.md                    (8,341 B) — 12 WUs across 2 PRs
├── verify-report.md            (16,015 B) — PR 1 outcome (PASS WITH WARNINGS, WARNING closed)
└── verify-report-2.md          (11,849 B) — PR 2 outcome (PASS, 0 CRITICAL)
```

All 8 entries are present in archive; nothing was dropped or modified during the move.

---

## Tasks Completion (Task Completion Gate)

12 WUs across 2 PRs. The persisted `tasks.md` does not use a Markdown checkbox format (`- [ ]` / `- [x]`); it uses `### WU x.y {name}` headers with explicit `Acceptance`, `Files`, and `Cmd` fields. Per the orchestrator launch prompt and the verify-reports below, all 12 WUs are functionally complete. The Task Completion Gate is satisfied via the `apply-progress`/`verify-report` proof path (the archive protocol's exceptional repair clause).

| WU | Title | PR | Acceptance evidence |
|----|-------|----|---------------------|
| 1.1 | conftest fixtures | PR 1 | `sample_catalog(vendor=, model=, firmware=)` kwargs + `tmp_builtin_root` fixture exposed — locked by `TestMultiRoot` cases. |
| 1.2 | pin `packaging>=24.0` | PR 1 | `Version("1.2.3-rc.1").base_version == "1.2.3"` importable — verified by PR 1's toolchain run. |
| 1.3 | per-(vendor, model) REQUIRED_OIDS table | PR 1 | `_REQUIRED_OIDS_BY_VENDOR_MODEL[(cambium, pmp450i)]` covers 6 OIDs — locked by `test_required_oids_by_vendor_model_covers_pmp450i` + `test_required_oids_alias_matches_pmp450i_table`. |
| 1.4 | `verify(built_in_root, operator_root, signing_key)` | PR 1 | `verify()` returns registry covering both roots; `verify_all(settings)` thin wrapper. R1–R7 verbatim green. |
| 1.5 | deterministic two-root scan + override precedence | PR 1 | `sorted(iterdir)` + `sorted(rglob)`; operator overwrites built-in on `(v,m,f)` collision; dup-within-root → `CatalogVerificationError`. Scenarios `operator override wins` + `deterministic two-root scan` green. |
| 1.6 | fail-fast on invalid HMAC in any root | PR 1 | `_verify_one` reused; tampered file in either root aborts boot. `test_invalid_hmac_in_any_root_raises_catalog_verification_error` green. |
| 1.7 | ship built-in baseline via package data | PR 1 | `data/__init__.py` + `data/oid-catalogs/cambium/pmp450i/15.2.1.json` ship; `importlib.resources.files("nora.data.oid_catalogs")` resolves them. |
| 1.8 | parameterise `sign_catalog.py` | PR 1 | `--vendor/--model/--firmware` CLI args with backward-compat defaults; sign+verify roundtrip works for arbitrary triple — `tests/test_sign_catalog.py` 5 cases pass. |
| 2.1 | semver-aware resolve + pre-release strip | PR 2 | Exact match silent; `15.2.1-rc.1` → `15.2.1` without warning — locked by `test_exact_match_returns_without_warning` + `test_pre_release_request_matches_bare_version`. |
| 2.2 | strict-major hard fail | PR 2 | `16.0.0` vs `15.x` raises `CatalogNotFoundError(ref)` whose message names both majors — `test_major_mismatch_raises_typed_exception`. |
| 2.3 | minor descending fallback + literal warning | PR 2 | `15.3.1` vs `(15.2.1, 15.3.0)` returns `15.3.0`; `caplog.records[0].message == "OID catalog fallback: requested 15.3.1, using 15.3.0 (minor mismatch)"` exact — `test_minor_mismatch_returns_closest_lower_minor_with_literal_warning`. |
| 2.4 | reissue locking fixture | PR 2 | `test_unknown_firmware_raises_catalog_not_found` fixture unchanged (requests `99.0.0` vs `15.x`); assertion updated so message names both majors. Per-Firmware Pin wording removed. |

Total: **12/12 WUs complete**, all backed by named tests in the suite.

---

## Mechanical Copy Contract — readback evidence

### `oid-catalog` spec compose

- Source: `openspec/changes/.../specs/oid-catalog/spec.md` (per-domain delta written via Read→Write transform of the single-file `spec.md`; the transform was structural — extracting the `oid-catalog` capability section — not a verbatim copy of any other artifact).
- Compose: `gentle-ai sdd-archive-compose` → `spec.md.compose-tmp` (zero exit).
- `mv spec.md.compose-tmp spec.md` → zero exit, atomic.

The compose tool's zero exit is the composition evidence (per SKILL.md); the diff between canonical input and `compose-tmp` output shows exactly the expected delta: 2 MODIFIED requirement bodies, 1 REMOVED requirement block, 4 ADDED requirement blocks. Unrelated requirements (HMAC-SHA256, Key Rotation, No Vendor MIB Text, No Network) and the Cross-References section were preserved byte-for-byte by the compose tool.

### Change folder move

```
git mv "openspec/changes/2026-09-12-oid-catalog-hybrid-semver" \
       "openspec/changes/archive/2026-09-12-oid-catalog-hybrid-semver"
EXIT: 0
```

**Mandatory `diff -r` readback (source snapshot vs. destination; `archive-report.md` excluded as additive-only):**

```
$ diff -r "$snapshot_root/source" "$destination"
(empty — exit 0)
```

The snapshot was created before the `git mv` via `cp -R` into a fresh `mktemp -d` directory; the `EXIT` trap cleaned it up after the readback. Source directory was confirmed absent after the move; destination contains all 7 source entries (proposal, spec, design, tasks, verify-report, verify-report-2, plus the `specs/` subfolder added by the compose-prep step) byte-identical to the pre-move snapshot.

### Post-move git state

```
$ git status --short
R  openspec/changes/2026-09-12-oid-catalog-hybrid-semver/design.md -> openspec/changes/archive/2026-09-12-oid-catalog-hybrid-semver/design.md
R  openspec/changes/2026-09-12-oid-catalog-hybrid-semver/proposal.md -> openspec/changes/archive/2026-09-12-oid-catalog-hybrid-semver/proposal.md
R  openspec/changes/2026-09-12-oid-catalog-hybrid-semver/spec.md -> openspec/changes/archive/2026-09-12-oid-catalog-hybrid-semver/spec.md
R  openspec/changes/2026-09-12-oid-catalog-hybrid-semver/tasks.md -> openspec/changes/archive/2026-09-12-oid-catalog-hybrid-semver/tasks.md
R  openspec/changes/2026-09-12-oid-catalog-hybrid-semver/verify-report-2.md -> openspec/changes/archive/2026-09-12-oid-catalog-hybrid-semver/verify-report-2.md
R  openspec/changes/2026-09-12-oid-catalog-hybrid-semver/verify-report.md -> openspec/changes/archive/2026-09-12-oid-catalog-hybrid-semver/verify-report.md
 M openspec/specs/oid-catalog/spec.md
?? openspec/changes/archive/2026-09-12-oid-catalog-hybrid-semver/specs/
?? .pi/                                  (pre-existing untracked, out of scope)
?? data/devices-tmp.yaml                 (pre-existing untracked, out of scope)
```

All 6 archive renames tracked by git; 1 spec modified; the `specs/` subfolder added by the compose-prep step is untracked (it is a working artifact of the archive phase, not a tracked change artifact).

---

## Cross-File Consistency (final state)

| Check | Result | Detail |
|-------|--------|--------|
| Active `openspec/changes/` contains this change | ✅ No | Only `.gitkeep` + `archive/` remain. |
| Archive contains all artifacts | ✅ Yes | proposal, spec (single-file), design, tasks, verify-report, verify-report-2 + audit-trail `specs/` + this archive-report. |
| `tasks.md` shows all 12 WUs completed | ✅ Yes | 12 `### WU x.y` headers present; completion proven by `verify-report.md` (PR 1: 294 pass, all P1 acceptance criteria locked) and `verify-report-2.md` (PR 2: 298 pass, all P2 acceptance criteria locked). |
| `verify-report.md` shows 0 CRITICAL | ✅ Yes | "PASS WITH WARNINGS" — 1 WARNING (tasks.md documentation drift), now closed per orchestrator. |
| `verify-report-2.md` shows 0 CRITICAL | ✅ Yes | "PASS" — 0 CRITICAL, 0 WARNING, 2 SUGGESTIONS. |
| `oid-catalog/spec.md` updated with delta | ✅ Yes | 10 requirements (was 7): 4 ADDED, 2 MODIFIED, 1 REMOVED, 4 preserved. Cross-References preserved. |
| `driver-snmp-pmp450i/spec.md` updated | ✅ N/A | Delta was wording-only; no formal requirement delta. |
| `prompt-registry/spec.md` updated | ✅ N/A | Delta was cross-reference-only; no formal requirement delta. |
| Verbatim `diff -r` readback output in phase result | ✅ Yes | Empty output (exit 0) recorded above. |

---

## Open Follow-ups (informational, not blocking archive)

These are the 2 SUGGESTIONS from `verify-report-2.md`. They are non-blocking by definition; the archive proceeds.

1. **Multi-minor regression test**: A test that exercises a `(15.2.1, 15.2.3, 15.3.0)` registry with three different request firmwares would lock determinism in the named suite. The current `TestSemverResolution` cases cover exact / pre-release / major / single-minor-fallback; ad-hoc trace in `verify-report-2.md` (test 8) confirmed the implementation but a named regression test is missing. Tracked for a future PR.
2. **`+build` metadata path untested**: Pass 2 of the resolver strips `+build.5` via `Version(...).base_version`, but no named test exercises the `+build` path. The `packaging>=24.0` dependency guarantees the behaviour; covered implicitly.

Neither requires a spec change; both can land as additive tests in a future change.

---

## SDD Cycle Complete

`2026-09-12-oid-catalog-hybrid-semver` has been fully planned (`proposal.md`), specified (`spec.md`), designed (`design.md`), task-planned (`tasks.md`, 12 WUs / 2 PRs), implemented (PRs #18 and #19 merged to `main`), verified (`verify-report.md`, `verify-report-2.md` — both PASS, 0 CRITICAL final state), and archived (this report).

The source of truth for OID catalog resolution now lives in `openspec/specs/oid-catalog/spec.md` and reflects the multi-root layered-load + semver-aware resolution model. ADR #17's 8 named acceptance tests (P1.1–P1.4 + P2.5–P2.8) are all locked by named regression tests in `tests/test_oid_catalog.py`. The next change can build on this foundation — multi-vendor / multi-protocol work tracked under issues #14 and #15 is unblocked.

**Status**: complete. Ready for the next change.
