# Proposal: Foundation Bootstrap Cleanup (Phase 1 Wrap-up)

## Intent

Close W1/W3 warnings + R1/R4 config drift from `openspec/changes/archive/2026-09-06-foundation-bootstrap/verify-report.md` so Phase 1 ships clean before Phase 2 (driver layer). No new capability. Each change covers a PARTIAL scenario with a test, tightens `_SDK_ERROR_MAP`, or refreshes SDD config. Production-code edits ≤ 5 lines.

## Scope

### In Scope

1. **W1 — close 4 PARTIAL scenarios** (verify-report 56, 61, 72, 90):
   - `project-toolchain` Discoverable Make: `make -n {test,lint,format,type,run}` asserts each invokes the expected tool.
   - `nora-mcp-server` error-path: raise carrying IPv4 + MAC + serial + hostname + fake key; assert response + stderr leak nothing.
   - `telemetry-sanitizer` alias-map log-level: `logger.debug(...)` in `Sanitizer._alias_for`; test asserts counter at DEBUG, absent at INFO.
   - `secure-configuration` `model_id` untouched: SDK recorder proves `Completion.model_id` reaches SDK verbatim.
2. **W3 — explicit `_SDK_ERROR_MAP`** (line 158): replace `(Exception,)` with `lmstudio.{LMStudioError,LMStudioTimeoutError,LMStudioPredictionError,LMStudioClientError}` + `google.genai.errors.{APIError,ClientError,ServerError}`. Tests: each → `LLMUnavailable`; `KeyboardInterrupt`/`SystemExit` NOT caught.
3. **R1 — refresh `openspec/config.yaml` prose** (lines 10-92): rewrite `context:`/`testing:` for Python 3.12, hatchling, pytest 8.x, ruff, mypy --strict, pytest-cov. SDD keys (`strict_tdd`, `verify.test_command`, `verify.build_command`, `apply.test_command`, `apply.tdd`, `testing.strict_tdd`) byte-identical.
4. **R4 — wire `pytest-cov`** (line 124): `verify.test_command: uv run python -m pytest --cov=src/nora --cov-report=term-missing`; `coverage_threshold: 85`.

### Out of Scope

Driver layer (Phase 2), HITL `ChangeRequest` (Phase 3), additional MCP tools, runtime provider switching, module-boundary refactors, new dependencies. Changed LOC ≤ 250 (under `review_budget_lines: 800`).

## Capabilities

### New Capabilities

None.

### Modified Capabilities

None. W1 satisfies already-written scenarios; W3 is implementation tightening; R1/R4 are config-only. No delta spec.

## Approach

Strict TDD — test RED first, then minimum GREEN. Reuse `_venv_bin`/`_run` helpers, `caplog`, `mock.patch` on `nora.llm.lms`/`nora.llm.genai`. SDK exception list from verified `dir()` import (Engram `nora/sdk-error-classes`).

## Affected Areas

| Area | Impact | Description |
|------|--------|-------------|
| `src/nora/llm.py` (84-87) | Modified | Explicit `_SDK_ERROR_MAP`. |
| `src/nora/sanitizer.py` (`_alias_for`) | Modified | Add `logger.debug(...)`. |
| `openspec/config.yaml` (10-92, 122-124) | Modified | Refresh prose; add `--cov=src/nora`; threshold 85. |
| `tests/test_toolchain.py` | Modified | Make behaviour test. |
| `tests/test_server.py` | Modified | Error-path sanitization test. |
| `tests/test_sanitizer.py` | Modified | Alias-map log-level test. |
| `tests/test_llm.py` | Modified | `model_id` + per-class mapping tests. |

## Risks

| Risk | Lik | Mitigation |
|------|------|------------|
| LOC > 250. | Low | ~220 estimated; tracked in `sdd-tasks`. |
| Coverage < 85%. | Low | 103-test baseline stable; document in `coverage_note` if needed. |
| Prose refresh changes SDD key. | Low | Diff scoped to prose fields; SDD keys byte-checked in CI. |
| DEBUG log regresses capture. | Low | Stdlib `logging`; `caplog` already used in `test_sanitizer.py`. |

## Rollback Plan

Revert four commits (test-first order): 4 new tests → `_SDK_ERROR_MAP` → sanitizer debug log + alias test → `openspec/config.yaml` prose/coverage. Each self-contained.

## Dependencies

None new. `pytest-cov>=5`, `lmstudio>=1,<2`, `google-genai>=1,<3` already pinned.

## Success Criteria

- [ ] `pytest --cov=src/nora --cov-report=term-missing` exits 0; coverage ≥ 85%.
- [ ] 4 new tests pass; 4 PARTIAL scenarios flip to FULL.
- [ ] `ruff check .`, `ruff format --check .`, `mypy --strict src/nora` exit 0.
- [ ] `_SDK_ERROR_MAP` enumerates explicit classes; each → `LLMUnavailable`; `KeyboardInterrupt`/`SystemExit` NOT caught.
- [ ] `verify.test_command` includes `--cov=src/nora`; `coverage_threshold: 85`.
- [ ] Changed LOC ≤ 250; no IPs/MACs/serials/hostnames/credentials added.