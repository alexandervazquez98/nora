# Design: Secure-Configuration Spec Re-issue

## Technical Approach

Pure spec re-issue — no code, no new tests. The `nora-mcp-thin-split` archive's composer pitfall (deterministic `REMOVED → MODIFIED` ordering over a delta that listed the same requirements in both blocks — see `openspec/changes/archive/2026-09-12-nora-mcp-thin-split/specs/secure-configuration/spec.md`) silently dropped `Credentials Never Appear in String Representations` and `Settings Load Status Is Observable` from `openspec/specs/secure-configuration/spec.md`. The implementation (`src/nora/config.py`) and tests (`tests/test_config.py` — 17 tests, all green at HEAD) survived intact.

The delta at `openspec/changes/2026-09-12-secure-config-reissue/specs/secure-configuration/spec.md` carries ONLY `## ADDED Requirements` (2 requirements) + `## Cross-References`. The orchestrator runs `gentle-ai sdd-archive-compose` to merge the delta into the canonical; no code path, no test path is touched.

## Architecture Decisions

### Decision: Re-issue via ADDED, not MODIFIED or RENAMED

| Option | Tradeoff | Decision |
|--------|----------|----------|
| ADDED (this change) | Preserves audit trail of the pitfall; canonical grows 4 → 6 | chosen |
| MODIFIED | Would re-trigger the same composer pitfall (REMOVED wins) | rejected |
| Manual edit of canonical | Bypasses `archive-compose`; loses the documented rollback | rejected |

**Rationale**: ADDED is the only verb that doesn't reintroduce the exact failure mode that caused the drop. The `...-reissue` suffix and the proposal's intent section document the pitfall for future authors.

### Decision: No code, no test changes

| Option | Tradeoff | Decision |
|--------|----------|----------|
| Re-issue spec only | Surviving tests already cover every new scenario | chosen |
| Add "spec coverage" tests | Redundant; would couple test count to spec count | rejected |
| Refactor `config.py` to match spec more literally | Spec mirrors the implementation; nothing to refactor | rejected |

**Rationale**: Every ADDED scenario is pinned to a literal already present in `tests/test_config.py` (`do-not-leak-this-key`, `hidden-secret-value`, `./data/from-dotenv/`, `/etc/nora/from-process-env/`). New tests would assert the same conditions twice.

## Data Flow

Unchanged. The `Settings` API surface (fields, `__init__`, `_detect_loaded_from`, `settings_customise_sources`) is intact in `src/nora/config.py`. Spec work does not touch runtime paths.

## File Changes

| File | Action | Description |
|------|--------|-------------|
| `openspec/changes/2026-09-12-secure-config-reissue/design.md` | Create | This document |
| `openspec/specs/secure-configuration/spec.md` | Modify (via archive-compose) | Grows 4 → 6 requirements + Cross-References section |
| `src/nora/config.py` | None | Already covers both contracts (`SecretStr` masking, `loaded_from`, `settings_customise_sources`) |
| `tests/test_config.py` | None | 17 tests remain the sole verification surface |

## Interfaces / Contracts

No new interfaces. The two ADDED requirements describe contracts already implemented:

- **`Credentials Never Appear in String Representations`** — enforced by `SecretStr` on `nora_oid_catalog_signing_key` (Pydantic v2 default `repr`).
- **`Settings Load Status Is Observable`** — enforced by `loaded_from: LoadSource = ".env" | "process_env" | "defaults"` and `settings_customise_sources` (dotenv swapped above env for matching keys).

## Testing Strategy

| Layer | What to Test | Approach |
|-------|-------------|----------|
| Unit | None new | Existing 17 tests in `tests/test_config.py` cover every ADDED scenario |
| Integration | None new | No new entry points |
| E2E | N/A | NORA has no browser-driven E2E (`openspec/config.yaml`) |

**Coverage map** (delta scenario → surviving test):

| Delta Scenario | Surviving Test |
|----------------|----------------|
| `repr(settings)` masks secrets | `test_repr_masks_secret_fields` |
| log lines never contain secrets | `test_log_lines_never_contain_secrets` |
| `.env` precedence over process environment | `test_env_file_takes_precedence_over_process_env` |
| `process_env` branch when no `.env` | `test_process_env_is_used_when_no_env_file` |
| defaults are explicit when nothing is set | `test_defaults_are_explicit_when_nothing_is_set` |

After merge, `uv run python -m pytest tests/test_config.py` MUST remain 17/17 green. Full suite MUST stay ≥ 85 % coverage.

## Threat Matrix

N/A — pure spec work. The change introduces no routing, shell commands, subprocesses, VCS/PR automation, executable-file classification, or process-integration boundary. `src/nora/config.py` and `tests/test_config.py` are not modified.

## Migration / Rollout

No migration. The merge is performed by `gentle-ai sdd-archive-compose`, which executes an atomic `mv` of the composed file into the canonical path.

**Rollback**: revert the merge commit. The composer `mv` is atomic — rolling back returns `openspec/specs/secure-configuration/spec.md` to its 4-requirement state. No code or test impact either direction.

**Failure mode to watch**: the composer's deterministic `REMOVED → MODIFIED` ordering over a delta that lists the same requirement in both blocks silently drops the surviving intent. Future SDD authors MUST NOT re-introduce REMOVED+MODIFIED for the same requirement. The `...-reissue` suffix and the proposal's intent section document the boundary. The new `Cross-References` block also pins the split with `nora-mcp-server > Security Boundary — No Secrets in Tool Responses` so the tool-response scenario (which the prior MODIFIED block had tried to roll into this domain) stays where it belongs.

## Open Questions

None. The change is bounded by the proposal's In Scope / Out of Scope split.