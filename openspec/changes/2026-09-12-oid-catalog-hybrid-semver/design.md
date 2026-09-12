# Design: 2026-09-12-oid-catalog-hybrid-semver

## Overview

`OidCatalogRegistry` becomes a layered loader (built-in baseline via `importlib.resources` + operator override via `Settings.oid_catalogs_path`, override wins on `(vendor, model, firmware)` conflict) and a semver-aware resolver (exact → pre-release-stripped → strict-major hard fail → minor descending fallback with literal telemetry warning). Mirrors `PromptRegistry.from_settings`; reuses `_verify_one` unchanged.

## Architecture

### Module Boundaries

```
src/nora/drivers/oid_catalog.py     ← MODIFIED: verify/verify_all/resolve + _REQUIRED_OIDS_BY_VENDOR_MODEL
src/nora/data/__init__.py           ← NEW: empty marker for importlib.resources
src/nora/data/oid-catalogs/         ← NEW: built-in baseline JSON per triple
scripts/sign_catalog.py             ← MODIFIED: --vendor/--model/--firmware CLI
pyproject.toml                      ← MODIFIED: packaging>=24.0 dep
tests/conftest.py                   ← MODIFIED: sample_catalog kwargs + tmp_builtin_root
tests/test_oid_catalog.py           ← MODIFIED: drop locking test, add 8 ADR #17 scenarios
```

Out of scope (locked for #14): `drivers/inventory.py`, `drivers/registry.py`, `drivers/exceptions.py`, `drivers/snmp_pmp450i/*`.

### Public API Changes

```python
_REQUIRED_OIDS_BY_VENDOR_MODEL: Final[dict[tuple[str, str], frozenset[str]]]
# REQUIRED_OIDS retained as derived alias for ("cambium", "pmp450i") — driver import untouched.

class OidCatalogRegistry:
    @classmethod
    def verify(
        cls, *, built_in_root: Path | None, operator_root: Path, signing_key: SecretStr | None,
    ) -> "OidCatalogRegistry": ...

    @classmethod
    def verify_all(cls, settings: Settings) -> "OidCatalogRegistry":
        # Thin wrapper; built_in_root = importlib.resources.files("nora.data.oid_catalogs")
        ...

    def resolve(self, ref: tuple[str, str, str]) -> OidCatalog:
        # Signature UNCHANGED. Internally: Version(...).base_version compare.
        ...
```

`resolve -> OidCatalog` (no `ResolveResult` wrapper) keeps `Pmp450iDriver._fetch_all` untouched — driver capability is "wording only". Warning = `logger.warning(...)`; the existing `server.configure_logging()` stderr handler is the telemetry channel. The sanitizer's 4 regex categories (private IP / MAC / serial / hostname) don't match semver literals, so routing through `Sanitizer` would be a no-op.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Built-in via `importlib.resources.files("nora.data.oid_catalogs")` | Mirrors `PromptRegistry._PACKAGED_PROMPTS_DIR`. Repo-path (cwd-dependent) and config-flag (doubles surface) rejected. |
| 2 | `packaging.version.Version` + `.base_version` strip | `Version("15.2.1-rc.1").base_version == "15.2.1"` satisfies "Pre-release and build metadata MUST be ignored". `packaging>=24.0` pinned. Custom `FirmwareVersion` rejected (reinvents edge cases). |
| 3 | Reuse `CatalogNotFoundError(ref)` with both majors named in the message | Matches spec wording. New subclass rejected (no operator need). |
| 4 | `_REQUIRED_OIDS_BY_VENDOR_MODEL` table; `REQUIRED_OIDS` = derived alias for `(cambium, pmp450i)` | Spec satisfied; driver untouched; #14 can swap the alias for `Device`-scoped lookup. |
| 5 | Sort each root (built-in: sorted `Traversable.iterdir`; operator: `sorted(root.rglob("*.json"))`); operator overwrites built-in on `(v,m,f)` collision; duplicate within one root → `CatalogVerificationError` | Deterministic regardless of filesystem race. |
| 6 | Warning = `logger.warning("OID catalog fallback: requested %s, using %s (minor mismatch)", req, chosen)` | Stderr handler from `configure_logging()` is the existing telemetry channel; semver literal isn't matched by sanitizer's 4 regexes. `ResolveResult(catalog, warning)` rejected (forces call-site change in `Pmp450iDriver`). |
| 7 | `_verify_one` reused verbatim across both roots | HMAC + REQUIRED_OIDS + schema logic is per-file and root-agnostic. Existing R3 / R4 / R5 stay green (no built-in in their hermetic settings). |

## PR Slice Plan (auto-chain)

### PR 1: p1-hybrid-loading

**Includes** — `src/nora/data/__init__.py`; `src/nora/data/oid-catalogs/cambium/pmp450i/15.2.1.json` (built-in, signed via `scripts/sign_catalog.py`); `packaging>=24.0`; `_REQUIRED_OIDS_BY_VENDOR_MODEL` + `REQUIRED_OIDS` alias; new `OidCatalogRegistry.verify(...)`; `verify_all(settings)` refactored to delegate; `scripts/sign_catalog.py` CLI args (defaults preserve backward compat); conftest `sample_catalog` kwargs + `tmp_builtin_root`; 3 new tests.

**Excludes** — `resolve(...)` semantics change. Existing R1-R7 untouched.

**Merge criteria** — 10 existing + 3 new tests pass; `ruff` + `mypy --strict` green.

### PR 2: p2-semver-resolution

**Includes** (depends on PR 1) — `resolve(ref)` rewritten with `Version(...).base_version`; strict-major hard fail with both majors in `CatalogNotFoundError`; minor descending fallback + `logger.warning` literal; `test_unknown_firmware_raises_catalog_not_found` reissued (fixture unchanged, message assertion updated); 4 new tests (`caplog` for warning capture).

**Excludes** — new vendors/models; `Literal["cambium"]` widening (#14).

**Merge criteria** — 8 new scenarios pass; existing 10 still pass; `caplog.records[0].message == "OID catalog fallback: requested 15.3.1, using 15.3.0 (minor mismatch)"` exactly. PR 1's operator-only path is bit-identical (no built-in in test fixtures → `verify(...)` matches pre-PR1 `verify_all(...)`), so PR 2 regressions surface in TDD, not after merge.

## Test Architecture

| Layer | Coverage | Location |
|-------|----------|----------|
| Unit (preserved) | R1–R7 verbatim | `tests/test_oid_catalog.py` |
| Unit (PR 1) | Operator-wins; deterministic two-root scan; invalid-HMAC-in-any-root | `TestMultiRoot*` |
| Unit (PR 2) | Exact-no-warning; pre-release-stripped; major-mismatch-typed; minor-mismatch-warning | `TestSemverResolution*` |
| Reissued (PR 2) | Locking fixture unchanged; message updated for both majors | `test_unknown_firmware_raises_catalog_not_found` |
| Hermetic | `sample_catalog` kwargs + `tmp_builtin_root` | `tests/conftest.py` |

`caplog.records[0].message` must equal the literal exactly per ADR #17 P2.

## Risk Mitigations

| Risk | Mitigation |
|------|------------|
| Semver mishandles pre-release / build | `packaging>=24.0` + named test for pre-release AND `+build` |
| Global `REQUIRED_OIDS` blocks second vendor | Per-`(vendor,model)` table ships now; second vendor = additive entry |
| `Literal["cambium"]` widening breaks openchat | `Device` untouched in this change |
| Override vs built-in duplication silent | Named test + duplicate-within-root → `CatalogVerificationError` |

## Open Questions

None. The literal warning string is locked by the spec scenario `Minor Descending Fallback With Literal Warning`; any future change to the string must update that scenario in lockstep.