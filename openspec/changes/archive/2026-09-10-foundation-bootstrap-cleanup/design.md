# Design: foundation-bootstrap-cleanup

## Technical Approach

Close the four W1 PARTIAL scenarios with one work-unit commit per capability (RED→GREEN), tighten `_SDK_ERROR_MAP` (W3), and refresh `openspec/config.yaml` prose + raise `coverage_threshold` (R1+R4). All production edits live in `src/nora/llm.py` and `src/nora/sanitizer.py` and stay under the 5-line budget per file. Strict TDD: every change preceded by a failing test in the same commit. No spec delta (case A confirmed in `specs/README.md`).

## Architecture Decisions

| # | Option (vs considered) | Tradeoff | Chosen |
|---|------------------------|----------|--------|
| 1 | Sanitizer DEBUG log site: `_alias_for` (per-literal) vs `sanitize()` (per-replacement) | `_alias_for` fires once per *distinct* literal — bounded log volume; `sanitize()` fires every match. | **`_alias_for`** |
| 2 | SDK-error per-class test shape: `pytest.mark.parametrize` vs one test per class | Parametrize mirrors `test_sanitizer.py::test_non_string_input_raises_sanitizer_input_error` style: single failure surface, readable diff. | **`pytest.mark.parametrize`** |
| 3 | `make -n` invocation: `subprocess.run` via `_run` vs `shutil.which`+raw subprocess | `_run` already wraps cwd + capture for this repo (`test_pytest_coverage_table_for_src_nora_is_printed`). | **`subprocess.run` via `_run`** |
| 4 | MCP error-path trigger: `monkeypatch(provider.complete.side_effect=LLMUnavailable(payload))` vs direct tool call with bad provider | Extends `_call_nora_health` seam used by `test_health_sanitizes_upstream_error_messages`; `capfd` captures stderr to assert no leak. | **monkeypatch + capfd** |
| 5 | `openspec/config.yaml` shape | Six SDD keys (`testing.strict_tdd`, `apply.tdd`, `apply.test_command`, `verify.build_command`, plus key names) stay byte-identical; `verify.test_command` value changes per R4 only. | Prose rewrite + surgical R4 edit |
| 6 | `test_pytest_failure_messages_are_captured_and_visible` deviation | Prior verify-report accepted stdout-OR-stderr as known acceptable; touching would churn a passing test for no behaviour gain. | **Do not touch.** |

## Data Flow — `_SDK_ERROR_MAP` exception→`LLMUnavailable`

```
 SDK call (lmstudio.Client / google.genai.Client)
   │ raises typed error
   ▼
 except _SDK_ERROR_MAP as exc:   ── 7-type tuple:
   lmstudio.LMStudioError (base;   ── catches Timeout/Prediction/Client + 8
   lmstudio.LMStudioTimeoutError,    others via isinstance)
   lmstudio.LMStudioPredictionError,
   lmstudio.LMStudioClientError,
   google.genai.errors.APIError,
   google.genai.errors.ClientError,
   google.genai.errors.ServerError  ── KeyboardInterrupt/SystemExit: NOT caught
   ▼
 _to_unavailable(exc)             ── logger.warning(class=%s message=%s, ...)
   ▼ raises
 LLMUnavailable(f"{type(exc).__name__}: {exc}")
   ▼
 caller (server.py / factory consumer)
```

## File Changes

| File | Action | Net | Description |
|------|--------|-----|-------------|
| `tests/test_toolchain.py` | Modify | +~25 | `test_makefile_targets_invoke_configured_tools` — `make -n {test,lint,format,type,run}` via `_run`; assert each expands to its tool. |
| `tests/test_llm.py` | Modify | +~30 | Parametrized `_SDK_ERROR_MAP` tests (4 lmstudio + 3 genai); `model_id` recorder; KbdInt/SysExit not-caught. |
| `src/nora/llm.py` | Modify | **+3** | Promote `import lmstudio as lms` + add `from google.genai import errors as genai_errors` at module level; rewrite 3-line tuple body. Within budget. |
| `src/nora/sanitizer.py` | Modify | **+2** | Add `logger = logging.getLogger(__name__)`; add `logger.debug("alias %s=%s", literal, alias)` in `_alias_for`. |
| `tests/test_sanitizer.py` | Modify | +~15 | `test_alias_map_debug_log_lists_literal_and_alias` (caplog at DEBUG asserts counter present) + `test_alias_map_absent_at_info_level` (caplog at INFO asserts absent). |
| `tests/test_server.py` | Modify | +~25 | Payload carries IPv4+MAC+serial+hostname+fake-key; `side_effect=LLMUnavailable(payload)`; fresh `_sanitizer` patched; assert response JSON + stderr clean. |
| `openspec/config.yaml` | Modify | rewrite | See diff below. |

### `openspec/config.yaml` — R1 prose refresh + R4 coverage

Rewrite `context:` (Python 3.12, hatchling, real `src/nora/`) and `testing:` (pytest-cov pinned, mypy --strict). Surgical edits:

```diff
   verify:
-    test_command: uv run python -m pytest
+    test_command: uv run python -m pytest --cov=src/nora --cov-report=term-missing
     build_command: uv run ruff check . && uv run ruff format --check . && uv run mypy --strict src/nora
-    coverage_threshold: 0
-    coverage_note: >-
-      Threshold stays 0 until pytest-cov is installed and pinned in the manifest;
-      raise it in the change that adds coverage.
+    coverage_threshold: 85
+    coverage_note: >-
+      Threshold raised to 85% now that pytest-cov is pinned in
+      [dependency-groups].dev and wired into verify.test_command via R4 of
+      foundation-bootstrap-cleanup. Edit when the suite grows.
```

Keys byte-identical: `strict_tdd`, `apply.tdd`, `apply.test_command`, `verify.build_command`, `testing.strict_tdd`; `verify.test_command` key name holds, only its value changes (R4).

## Interfaces / Contracts

No public API change. `_SDK_ERROR_MAP` is module-private. `Sanitizer._alias_for` emits one DEBUG record per `(category, literal)` pair — no INFO/WARNING emitted. Tuple annotation `tuple[type[BaseException], ...]` already satisfies `mypy --strict`.

## Testing Strategy

| Layer | What | Approach |
|-------|------|----------|
| Unit (toolchain) | `make -n` expands to configured tool | `_run([make, "-n", target])`; assert token in stdout |
| Unit (server) | secrets-leak in error path | `monkeypatch` provider; `_call_nora_health`; `capfd` |
| Unit (sanitizer) | DEBUG-only alias log | `caplog.at_level(DEBUG/INFO)`; presence/absence |
| Unit (llm) | per-class `_SDK_ERROR_MAP` | `pytest.mark.parametrize` × 7 classes → `LLMUnavailable` |
| Unit (llm) | `model_id` verbatim | SDK recorder fake `Client`; assert `model=` kwarg |
| Unit (llm) | `KeyboardInterrupt`/`SystemExit` not caught | raise each; expect bubble, NOT `LLMUnavailable` |

## Threat Matrix

**N/A — no routing/shell/subprocess/VCS/process-integration boundary.** `make -n` is a read-only dry-run, not an untrusted-input boundary.

## Migration / Rollout

No migration, no feature flag. CI runs `uv run python -m pytest --cov=src/nora --cov-report=term-missing` from R4 onwards. Existing `test_pytest_coverage_table_for_src_nora_is_printed` keeps semantics; no edit.

## Open Questions

None.
