# Verify Report: 2026-09-12-oid-catalog-hybrid-semver (PR 1)

## Summary

| Field | Value |
|-------|-------|
| Branch | `feat/p1-hybrid-loading` |
| HEAD | `9e60e1f9919afe89f75c9636bfcf4b00aedfccc5` |
| Verification date | 2026-09-12 |
| Scope | PR 1 — `p1-hybrid-loading` (two-root scan, built-in baseline, override precedence, fail-fast) |
| Strict TDD | Active (tests run first; failures are blockers) |
| Outcome | **PASS WITH WARNINGS** |

**Outcome rationale.** Every toolchain gate (pytest, ruff check, mypy --strict, ruff format) is clean; every PR 1 spec scenario passes; every ADR #17 named test (P1 coverage) passes; wheel packaging smoke test resolves the built-in baseline from a clean venv. Two findings — neither blocking, both forward-looking — recorded under WARNING: (1) the task-file "affected files" list does not name four integration test files that received the necessary `BUILTIN_BASELINE_SIGNING_KEY` swap; (2) one Tasks checkpoint expected `13 pass` for `test_oid_catalog.py` but actual is `20 pass` (PR 1 added 9 `TestMultiRoot` cases, not the originally-forecast 3). Both are documentation drift, not behavioural drift.

## Toolchain

| Check | Result | Detail |
|-------|--------|--------|
| `uv run pytest -q` | ✅ | **294 passed**, 2 skipped (pre-existing), 0 failed. ~71 s wall. |
| `uv run ruff check .` | ✅ | "All checks passed!" |
| `uv run mypy --strict src/nora` | ✅ | "Success: no issues found in 27 source files" |
| `uv run ruff format --check .` | ✅ | "62 files already formatted" |

`uv run pytest -q` exit code: 0. `pytest_output_hash` is not persisted by the harness but the test session header reports `294 passed, 2 skipped`.

### Test count reconciliation

The task file's PR 1 verification checkpoint expected "10 existing + 3 new `TestMultiRoot` = 13 pass" for `test_oid_catalog.py`. Actual is `20 pass` for `test_oid_catalog.py`. Reconciliation:

* 11 R1–R7 + ancillary cases (verbatim green, same as pre-PR1).
* 9 new `TestMultiRoot` cases (operator-wins, deterministic two-root scan, duplicate-within-root, invalid-HMAC-in-operator, invalid-HMAC-in-builtin, builtin-via-importlib-resources, missing-OID-per-triple, table-scoped-assertions, alias-derived-from-table).

The 9 vs 3 delta is additive test coverage (design-decision #5 split into dedicated `test_deterministic_two_root_scan`, `test_duplicate_within_root_raises_catalog_verification_error`, `test_invalid_hmac_in_any_root_raises_catalog_verification_error`, `test_invalid_hmac_in_builtin_raises_when_operator_root_empty`, plus the table-scoped + alias-derived assertions). No test was deleted. See WARNING-1 below.

## Spec Scenarios — PR 1 coverage

`Capability: oid-catalog` — four scenarios PR 1 must materialise:

| Scenario | Result | Test | Notes |
|----------|--------|------|-------|
| operator override wins on conflict | ✅ | `TestMultiRoot::test_operator_wins` | Both roots hold `15.2.1`; `signalStrengthRx` sentinel proves operator copy survived. |
| deterministic two-root scan with override precedence | ✅ | `TestMultiRoot::test_deterministic_two_root_scan` | Two back-to-back `verify(...)` calls yield identical `loaded_refs` (3 distinct triples after collision shadowing). |
| invalid HMAC in any root aborts boot | ✅ | `TestMultiRoot::test_invalid_hmac_in_any_root_raises_catalog_verification_error` AND `test_invalid_hmac_in_builtin_raises_when_operator_root_empty` | Both directions covered; `_verify_one` reused; exception's `path` points at the offending file. |
| missing OID fails verification per triple | ✅ | `TestMultiRoot::test_missing_oid_fails_per_triple` | Strips `radioDownlinkRate` and re-signs; `CatalogVerificationError` names the missing key. |

Scenarios deferred to PR 2 (out of scope for this verify, recorded for the orchestrator's awareness):

* `exact match returns without warning` — `resolve()` keeps exact-pin behaviour pre-PR2.
* `pre-release request matches the bare version` — `packaging>=24.0` is shipped; `Version(...).base_version` semantics land in PR 2.
* `major mismatch raises a typed exception` — PR 2's `strict_major` clause.
* `minor mismatch returns closest lower minor with literal warning` — PR 2's fallback + `logger.warning`.

PR 1's only contract is two-root loading + override precedence + fail-fast; the four scenarios above are precisely PR 1's surface.

## ADR #17 Named Tests — PR 1 coverage

| ADR #17 acceptance test | Result | Implementation |
|-------------------------|--------|----------------|
| **P1.1** Override gana sobre built-in | ✅ | `TestMultiRoot::test_operator_wins` |
| **P1.2** Built-in sin override funciona | ✅ | `TestMultiRoot::test_builtin_baseline_loads_via_importlib_resources` (resolves `nora.data.oid-catalogs` and verifies HMAC against `BUILTIN_BASELINE_SIGNING_KEY`) |
| **P1.3** Catálogo sin firma válida = fail-fast | ✅ | `TestMultiRoot::test_invalid_hmac_in_any_root_raises_catalog_verification_error` + `test_invalid_hmac_in_builtin_raises_when_operator_root_empty` |
| **P1.4** Precedencia determinista sin race conditions | ✅ | `TestMultiRoot::test_deterministic_two_root_scan` |

P2 named tests (`firmware idéntico sin warning`, `minor mismatch fallback + literal warning`, `major mismatch hard fail`, `múltiples minors firmados determinístico`) are **out of scope for PR 1** and are not validated here — they are PR 2's contract per the design doc and `tasks.md` slice plan.

Integración con #14/#15 named tests (`resolución dinámica de dispositivo`, `catálogo unificado`) are PR 2 scope per the design doc and were not attempted here.

## Design Decisions Compliance

The design doc enumerates seven decisions; PR 1 implements the four in scope and ships the packaging pin that PR 2 needs:

| # | Decision | PR 1 in scope? | Status | Evidence |
|---|----------|----------------|--------|----------|
| 1 | Built-in via `importlib.resources.files("nora.data.oid_catalogs")` | Yes | ✅ | `src/nora/drivers/oid_catalog.py` `OidCatalogRegistry.verify_all()` calls `files("nora.data.oid-catalogs")` (note hyphen — namespace package PEP 420). `src/nora/data/__init__.py` is the marker; `src/nora/data/oid-catalogs/cambium/pmp450i/15.2.1.json` is the baseline. |
| 2 | `packaging.version.Version` + `.base_version` strip (PR 2 logic) | Pin only | ✅ | `pyproject.toml` line 29: `"packaging>=24.0"` added. `uv run python -c "from packaging.version import Version; assert Version('1.2.3-rc.1').base_version == '1.2.3'"` exits 0. The actual `resolve()` rewrite lands in PR 2. |
| 3 | Reuse `CatalogNotFoundError(ref)` naming both majors | PR 2 | ⏭️ | PR 2 scope. |
| 4 | `_REQUIRED_OIDS_BY_VENDOR_MODEL` table; `REQUIRED_OIDS` = derived alias | Yes | ✅ | `oid_catalog.py` lines 53–69: `_REQUIRED_OIDS_BY_VENDOR_MODEL[("cambium", "pmp450i")]` and `REQUIRED_OIDS = _REQUIRED_OIDS_BY_VENDOR_MODEL[("cambium", "pmp450i")]`. Locked by `TestMultiRoot::test_required_oids_by_vendor_model_covers_pmp450i` and `test_required_oids_alias_matches_pmp450i_table`. |
| 5 | Deterministic sort + override-wins | Yes | ✅ | `oid_catalog.py` `_iter_json_files()` uses `sorted(root.iterdir(), key=lambda entry: entry.name)` at every level. `verify()` walks built-in first, then operator (overwrites via dict assignment on collision). Locked by `test_deterministic_two_root_scan` and `test_operator_wins`. Within-root duplicate → `CatalogVerificationError` (locked by `test_duplicate_within_root_raises_catalog_verification_error`). |
| 6 | Warning = `logger.warning(...)` with the literal string | PR 2 | ⏭️ | PR 2 scope. |
| 7 | `_verify_one` reused verbatim across both roots | Yes | ✅ | `oid_catalog.py` lines 172 and 186: both `for path in _iter_json_files(...)` loops call `cls._verify_one(path, key_bytes)`. Signature widened to accept `Path | Traversable` (3.12 vs 3.14 importlib split handled in `if sys.version_info >= (3, 14):` block) but the HMAC + REQUIRED_OIDS + schema logic is the same code path. Symmetric fail-fast covered by `test_invalid_hmac_in_builtin_raises_when_operator_root_empty`. |

All four PR 1-in-scope decisions are implemented and exercised by named tests.

## Bit-Identical Guarantee for PR 2

PR 2 depends on the operator-only path of PR 1 being bit-identical to pre-PR1 single-root loading. The proof is that every pre-PR1 test migrated to `OidCatalogRegistry.verify(built_in_root=None, operator_root=..., signing_key=...)` with the same fixtures, same canonicalisation, same key — and they all still pass.

| Gate | Result | Detail |
|------|--------|--------|
| `uv run pytest tests/test_oid_catalog.py -k "not TestMultiRoot"` | ✅ | 11/11 pass (all R1–R7 + ancillary) |
| `test_unknown_firmware_raises_catalog_not_found` | ✅ | Locking fixture requests `99.0.0` against an empty `tmp_catalogs_dir`; raises `CatalogNotFoundError`; the message contains both `"99.0.0"` and `"cambium"`. The `built_in_root=None` migration keeps it bit-identical to pre-PR1's `verify_all(settings)` with empty operator path. |
| Operator-only path equivalence | ✅ | All 11 R1–R7 cases migrated from `verify_all(_build_settings(...))` to `verify(built_in_root=None, operator_root=tmp_catalogs_dir, signing_key=...)`. No semantic change: `verify_all(settings)` itself is the thin wrapper that delegates to `verify(built_in_root=files("nora.data.oid-catalogs"), ...)` — pre-PR1's behaviour with an empty built-in matches the new `verify(built_in_root=None, ...)` path exactly. |

PR 2's regression surface is therefore isolated to the new `resolve()` rewrite — anything that breaks R1–R7 would show up in TDD before merge, not after.

## Wheel Packaging

Built the wheel into `dist/nora-0.1.0-py3-none-any.whl`, installed it into a clean venv at `/tmp/nora-wheel-venv`, and verified the smoke test.

| Gate | Result | Detail |
|------|--------|--------|
| `uv build` | ✅ | `Successfully built dist/nora-0.1.0.tar.gz` and `Successfully built dist/nora-0.1.0-py3-none-any.whl` |
| `importlib.resources.files("nora.data.oid-catalogs")` resolves in wheel mode | ✅ | Returns a `MultiplexedPath` rooted at `…/site-packages/nora/data/oid-catalogs`; recursive walk finds `cambium/pmp450i/15.2.1.json`. |
| `OidCatalogRegistry.verify(built_in_root=<wheel path>, ...)` succeeds against shipped baseline | ✅ | `loaded_refs == [('cambium', 'pmp450i', '15.2.1')]`; `catalog.oids['ssr'] == '1.3.6.1.4.1.161.19.3.1.1.5.0'`. |

The build artefacts were cleaned up (`rm -rf /tmp/nora-wheel-venv dist`); they are reproducible from the same commit on demand.

## Cross-File Consistency

| Check | Result |
|-------|--------|
| Scope creep in `test_server.py` / `test_integration.py` / `test_integration_boot.py` / `test_main_alias.py` (changes other than `BUILTIN_BASELINE_SIGNING_KEY`) | ✅ No creep — **all 4 files' edits are exclusively the signing-key swap** (`test-integration-key` → `BUILTIN_BASELINE_SIGNING_KEY`, `test-boot-key` → `BUILTIN_BASELINE_SIGNING_KEY`, `test-alias-key` → `BUILTIN_BASELINE_SIGNING_KEY`, `test-server-subprocess-key` → `BUILTIN_BASELINE_SIGNING_KEY`). Each substitution is annotated with a comment explaining pre-PR1 vs post-PR1 semantics. |
| Unlisted files modified (relative to `tasks.md` affected-files column) | ⚠️ Yes — see WARNING-1. |
| Planning artifacts touched (`openspec/changes/2026-09-12-oid-catalog-hybrid-semver/*` committed accidentally) | ✅ No — `git status` shows the change dir as untracked; nothing under `openspec/` was modified by any PR 1 commit. |

## Findings

### CRITICAL (blockers — must fix before merge)

*None.* Every toolchain gate is green, every PR 1 spec scenario passes, every ADR #17 P1 named test is locked, the design decisions in scope are implemented, the bit-identical guarantee for PR 2 is established, and the wheel packaging smoke test resolves the shipped baseline from a clean venv.

### WARNING (must address but not blocking)

- **WARNING-1 — `tasks.md` "affected files" list omits four integration tests that received a necessary `BUILTIN_BASELINE_SIGNING_KEY` swap.**
  `tests/test_integration.py` (+17), `tests/test_integration_boot.py` (+7), `tests/test_main_alias.py` (+8), `tests/test_server.py` (+13) are not in any WU's "Files" column, yet they had to adopt the placeholder built-in key to keep subprocess-booted tests passing post-PR1. The changes themselves are scoped and minimal (signing-key swap only), so this is documentation drift, not actual scope creep. Action: in PR 2 (or in a follow-up docs commit on PR 1), extend the WU 1.7 (or WU 1.6) "Files" column to include the four boot-subprocess tests so the next reviewer sees the full footprint.

### SUGGESTION (nice to have)

- **S1 — `tasks.md` PR 1 verification checkpoint understates the test count.** The WU 1.4 "Acceptance" mentions "13 pass" and the "Verification" line says "10 existing + 3 new `TestMultiRoot` = 13". Actual is 20 (`11 R1–R7 + ancillary` + `9 TestMultiRoot`). The 9 vs 3 split is additive coverage from design #4 / #5 / #7 (table scoping, alias derivation, within-root duplicate guard, symmetric invalid-HMAC, etc.) — not regression. Update the checkpoint to "13 existing + 9 new `TestMultiRoot` = 22 pass" or recount.
- **S2 — `BUILTIN_BASELINE_SIGNING_KEY` is a placeholder and should be rotated before any production deploy.** The module docstring on `src/nora/data/__init__.py` already documents the rotation workflow; this is informational, not an action item.

## Verdict

**PR 1 is READY for merge.**

All CRITICAL gates pass. The single WARNING is documentation drift (`tasks.md` affected-files list), not behavioural drift — the four extra file edits are minimal signing-key swaps that any reviewer will recognise as transitive adaptations to the new built-in HMAC verification. SUGGESTIONS are forward-looking and do not gate the merge.

The PR 2 slice is safe to start once this PR lands: every operator-only test still uses the same fixtures and canonicalisation, `_verify_one` is reused verbatim across both roots, and the `packaging>=24.0` pin is in place for PR 2's `Version(...).base_version` rewrite of `resolve()`.

## Next Steps

1. **Merge PR 1** (`feat/p1-hybrid-loading` → `main`) under the `stacked-to-main` chain strategy.
2. **Apply WARNING-1**: amend `tasks.md` PR 1 WU 1.7 (or WU 1.6) "Files" column to include the four integration test files. Either as part of the PR 1 follow-up commit (preferred — keeps the verification report in lockstep with the task ledger) or in the first commit of PR 2 (acceptable — the docstring drift does not block PR 2's TDD).
3. **Start PR 2** (`p2-semver-resolution`) on a fresh `feat/p2-semver-resolution` branch off `main` once PR 1 lands. The bit-identical operator-only path is the regression surface to lock first; WU 2.1's `packaging.version.Version` test is the green light to begin.
4. **Optional**: apply SUGGESTION-1 (test-count recount) when updating the tasks.md.

## Relevant Files (for the orchestrator's follow-up)

- `openspec/changes/2026-09-12-oid-catalog-hybrid-semver/verify-report.md` — this file.
- `openspec/changes/2026-09-12-oid-catalog-hybrid-semver/tasks.md` — needs the WARNING-1 fix (4 file additions to WU 1.7 "Files" column).
- `src/nora/drivers/oid_catalog.py` — PR 1 surface; bit-identical for operator-only path.
- `src/nora/data/__init__.py` — `BUILTIN_BASELINE_SIGNING_KEY` placeholder + rotation workflow.
- `src/nora/data/oid-catalogs/cambium/pmp450i/15.2.1.json` — shipped baseline (HMAC against the placeholder key).
- `tests/test_oid_catalog.py` — 9 new `TestMultiRoot` cases; 11 R1–R7 verbatim green.
- `tests/test_sign_catalog.py` — 5 new cases; sign → verify round-trip.
- `tests/conftest.py` — `sample_catalog` kwargs + `tmp_builtin_root` fixture.
- `scripts/sign_catalog.py` — `--vendor` / `--model` / `--firmware` / `--output-root` / `--key`; defaults preserve pre-PR1 behaviour.
- `pyproject.toml` — `packaging>=24.0` pin; `filterwarnings` for the 3.12 vs 3.14 `Traversable` split.
