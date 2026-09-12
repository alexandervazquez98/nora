# Proposal: `2026-09-12-secure-config-reissue`

## Intent

Re-issue two contracts accidentally dropped from `openspec/specs/secure-configuration/spec.md` during the `nora-mcp-thin-split` archive. The composer’s deterministic `MODIFIED → REMOVED` ordering over a delta that listed the same requirements in both blocks deleted the surviving intent. The implementation (`src/nora/config.py`) and tests (`tests/test_config.py`) are intact; only the spec is missing.

Restore the contracts as ADDED Requirements, scoped to *repr / log-line / observability*. The *tool-response* side already lives in `nora-mcp-server/spec.md > Security Boundary — No Secrets in Tool Responses` and is **out of scope**.

## Scope

### In Scope

- ADD `Credentials Never Appear in String Representations` — `repr(settings)`, `str(settings)`, log lines only.
- ADD `Settings Load Status Is Observable` — `loaded_from` and `.env` > process env > defaults precedence.
- ADD `## Cross-References` to `secure-configuration/spec.md` pointing at `nora-mcp-server/spec.md > Security Boundary — No Secrets in Tool Responses`.

### Out of Scope

- Tool-response secret-redaction (owned by `nora-mcp-server`).
- Any code change (`src/nora/config.py` intact).
- Any new test (17 existing tests already cover every new scenario).
- Any other change to the four surviving requirements.

## Capabilities

### New Capabilities
None.

### Modified Capabilities
- `secure-configuration`: ADD two requirements + cross-reference. No REMOVED, no RENAMED, no MODIFIED.

## Approach

Pure spec work. Write `openspec/changes/2026-09-12-secure-config-reissue/specs/secure-configuration/spec.md` with an `## ADDED Requirements` block. Each scenario pinned to the literal in the surviving test (`do-not-leak-this-key`, `change-me`, `NORA_OID_CATALOGS_PATH`). Run `gentle-ai sdd-archive-compose`; canonical grows 4 → 6 requirements plus cross-reference.

## Affected Areas

| Area | Impact |
|------|--------|
| `openspec/changes/2026-09-12-secure-config-reissue/specs/secure-configuration/spec.md` | New delta; two ADDED + cross-ref |
| `openspec/specs/secure-configuration/spec.md` | 4 → 6 requirements; cross-ref added |
| `tests/test_config.py` | Unchanged; 17/17 pass at HEAD |
| `src/nora/config.py` | Unchanged; already implements both contracts |

## Risks

| Risk | Lik | Mitigation |
|------|-----|------------|
| Future author re-introduces REMOVED+MODIFIED pitfall | Low | Proposal forbids REMOVED/MODIFIED; change named `…-reissue` for traceability |
| Tool-response scenario duplicated into `secure-configuration` | Low | Scope limits ADDED to repr + log lines; cross-reference documents boundary |
| Scenario wording drifts from surviving tests | Low | Each scenario pinned to the literal string in the existing test |

## Rollback Plan

Revert the merge commit. `sdd-archive-compose` mv is atomic; rolling back returns the canonical to 4 requirements. No code or test impact either way.

## Success Criteria

- [ ] Canonical `secure-configuration/spec.md` carries 6 requirements.
- [ ] `## Cross-References` names `nora-mcp-server > Security Boundary — No Secrets in Tool Responses`.
- [ ] `pytest tests/test_config.py` → 17/17 pass.
- [ ] Full pytest → coverage ≥ 85% (no regression).
- [ ] `ruff check . && ruff format --check . && mypy --strict src/nora` → all exit 0.
- [ ] No scenario in `secure-configuration/spec.md` duplicates any scenario in `nora-mcp-server/spec.md`.
