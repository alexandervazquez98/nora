# Tasks: Foundation Bootstrap Cleanup

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | ~220 (tests +5 LOC impl + config.yaml prose) |
| 800-line budget risk | Low |
| Chained PRs recommended | No |
| Suggested split | single PR (6 commits, one review slice) |
| Delivery strategy | ask-on-risk |
| Chain strategy | size-exception |

Decision needed before apply: No
Chained PRs recommended: No
Chain strategy: size-exception
800-line budget risk: Low

Rationale: ~220 estimated changed lines (3 new test files, 2 new tests in test_llm.py, +5 LOC production edits across `src/nora/llm.py` and `src/nora/sanitizer.py`, prose rewrite of `openspec/config.yaml`) sits well under the 800-line review budget and the 400-line per-PR guideline. All six commits are self-contained, independently testable, and revert without touching unrelated work; `delivery_strategy: ask-on-risk` therefore has nothing to ask. Single PR (`size-exception`) is the honest answer: splitting would fragment TDD pairs and force the reviewer to context-switch across work units that share no surface area.

## Phase 1: Toolchain discoverability (Commit 1)

- [x] 1.0 `test(toolchain): make -n assertions` — W1 #1
  - File: `tests/test_toolchain.py`
  - Acceptance: new `test_makefile_targets_invoke_configured_tools` runs `make -n {test,lint,format,type,run}` via `_run`; each expanded line contains its configured tool; skips when `make` absent.
  - Test cmd: `uv run python -m pytest tests/test_toolchain.py -k makefile_targets_invoke_configured_tools -v`
  - Test-only commit: Makefile already correct; the behaviour test was the PARTIAL gap.

## Phase 2: Server error-path sanitization (Commit 2)

- [x] 2.0 `test(server): nora_health error-path secrets-leak` — W1 #2
  - File: `tests/test_server.py`
  - Acceptance: payload carries IPv4 + MAC + serial + hostname + fake key; `side_effect = LLMUnavailable(payload)`; fresh `Sanitizer` patched via `mock.patch("nora.server._sanitizer", ...)`; `capfd` captures stderr; response `str()` and stderr contain no payload literal; response stays the 4-tuple with `connectivity == "unavailable"`.
  - Test cmd: `uv run python -m pytest tests/test_server.py -k health_error_path_does_not_leak -v`
  - Test-only commit: existing sanitization test covers one literal; 4-category payload + stderr assertion is the PARTIAL gap.

## Phase 3: Sanitizer DEBUG log contract (Commit 3, TDD)

- [x] 3.0 `test(sanitizer): alias-map DEBUG log contract` — W1 #3 (RED)
  - File: `tests/test_sanitizer.py`
  - Acceptance (RED): `test_alias_map_debug_log_lists_literal_and_alias` (caplog at DEBUG asserts literal+alias per `(category, literal)`) + `test_alias_map_absent_at_info_level` (caplog at INFO asserts zero alias-map records). Both FAIL until 3.1.
  - Test cmd (RED): `uv run python -m pytest tests/test_sanitizer.py -k alias_map -v` (must fail)
- [x] 3.1 `feat(sanitizer): DEBUG log in _alias_for` — W1 #3 (GREEN)
  - File: `src/nora/sanitizer.py`
  - Acceptance: `logger = logging.getLogger(__name__)` at module top; `logger.debug("alias %s=%s", literal, alias)` inside `_alias_for` after registering the new alias; net +2 lines; 3.0 passes; full `test_sanitizer.py` green.
  - Test cmd (GREEN): `uv run python -m pytest tests/test_sanitizer.py -v`

## Phase 4: LLM model_id verbatim (Commit 4)

- [x] 4.0 `test(llm): model_id verbatim through SDK` — W1 #4
  - File: `tests/test_llm.py`
  - Acceptance: two SDK-recorder tests. lmstudio: `client.llm.model(<id>)` receives `settings.lmstudio_model_id` verbatim. gemini: `client.models.generate_content(..., model=...)` kwarg is `settings.gemini_model_id` verbatim. Both pass without touching `src/nora/llm.py`.
  - Test cmd: `uv run python -m pytest tests/test_llm.py -k passes_model_id_verbatim_to_sdk -v`
  - Test-only commit: providers already pass `self._model_id`; existing test checks `Completion` field only, not the SDK kwarg required by `secure-configuration/spec.md:116-119`.

## Phase 5: Explicit _SDK_ERROR_MAP (Commit 5, TDD)

- [x] 5.0 `test(llm): explicit _SDK_ERROR_MAP per-class + not-caught` — W3 (RED)
  - File: `tests/test_llm.py`
  - Acceptance (RED): `@pytest.mark.parametrize` over 7 rows (`lmstudio.{LMStudioError,LMStudioTimeoutError,LMStudioPredictionError,LMStudioClientError}` + `google.genai.errors.{APIError,ClientError,ServerError}`) — each raises via existing `mock.patch("lmstudio.Client", ...)` / `mock.patch("google.genai.Client", ...)` seams and expects `LLMUnavailable`. Plus `test_keyboard_interrupt_is_not_caught` and `test_system_exit_is_not_caught` (`BaseException` subclasses bubble uncaught). RED until 5.1.
  - Test cmd (RED): `uv run python -m pytest tests/test_llm.py -k 'sdk_error_class or keyboard_interrupt or system_exit' -v` (must fail)
- [x] 5.1 `feat(llm): explicit _SDK_ERROR_MAP` — W3 (GREEN)
  - File: `src/nora/llm.py`
  - Acceptance: module-level `from google.genai import errors as genai_errors`; rewrite the 3-line tuple to enumerate the 7 classes; net +3 lines; `KeyboardInterrupt`/`SystemExit` (`BaseException`, not `Exception`) automatically not caught. Existing `test_lmstudio_provider_maps_sdk_errors_to_llm_unavailable` + gemini twin raise `RuntimeError` (now outside the tuple) — update them in this commit to raise typed SDK exceptions; mypy --strict passes.
  - Test cmd (GREEN): `uv run python -m pytest tests/test_llm.py -v`

## Phase 6: openspec config refresh (Commit 6)

- [x] 6.0 `docs(openspec): refresh config.yaml prose + wire pytest-cov` — R1+R4
  - File: `openspec/config.yaml`
  - Acceptance: `context:` reflects Python 3.12 + hatchling + real `src/nora/`; `testing:` reflects pytest-cov + mypy --strict + ruff; six SDD keys (`testing.strict_tdd`, `apply.tdd`, `apply.test_command`, `verify.build_command`, parents) byte-identical; `verify.test_command` gains `--cov=src/nora --cov-report=term-missing`; `coverage_threshold: 85`; `coverage_note` references R4.
  - Test cmd: `uv run python -m pytest --cov=src/nora --cov-report=term-missing` exits 0 with coverage ≥ 85%.

## Risks

| Risk | Mitigation |
|------|------------|
| Commit 5 narrows catch tuple; existing `RuntimeError` tests fall outside. | Update those two tests in commit 5 to raise typed SDK exceptions. |
| `coverage_threshold: 85` blocks merge if coverage slips. | 103-test baseline on `src/nora/` well above 85%; `coverage_note` is auditable signal to revise. |
| Config prose rewrite mutates an SDD key. | Six protected keys in `design.md` §D5; orchestrator byte-diffs before apply. |
| `make` absent on PATH. | Test skips via `shutil.which("make") is None`. |
| Sanitizer DEBUG log floods pytest output. | `caplog.at_level(DEBUG)` per-test scope; module logger has no handlers until `configure_logging()` runs. |

## Open Decisions

None. All six commits and budgets locked by `design.md` §D1–D6. No user weigh-in required before `sdd-apply`.