# Tasks: Toolchain Foundation for NORA (Phase 1)

## Review Workload Forecast

Decision needed before apply: Yes
Chained PRs recommended: Yes
Chain strategy: size-exception
400-line budget risk: High

### Suggested Work Units

| Unit | Goal | Focused test command | Runtime harness | Rollback boundary |
|------|------|----------------------|-----------------|-------------------|
| WU-1 | Toolchain + `secure-configuration` | `pytest tests/test_config.py -q` | `python3 -m nora` boots; `nora_health` -> `env_loaded=false` | Delete `pyproject.toml`, `uv.lock`, `.python-version`, `Makefile`, `.env.example`, `src/nora/{__init__,__main__,config}.py`, `tests/{conftest,test_smoke,test_config}.py` |
| WU-2 | `telemetry-sanitizer` | `pytest tests/test_sanitizer.py -q` | `Sanitizer().sanitize("x")` -> `SanitizedText` | Delete `src/nora/sanitizer.py` + test |
| WU-3 | LLM + MCP + e2e | `pytest tests/test_llm.py tests/test_server.py tests/test_integration.py -q` | `python3 -m nora` boots; `nora_health` round-trips via mocked provider | Delete `src/nora/{llm,server}.py` + tests |

## Phase 1: Toolchain Bootstrap (project-toolchain)

- [x] 1.1 RED - `tests/test_smoke.py` asserting `import nora` + `pytest --collect-only` exit 0; fails today.
- [x] 1.2 GREEN - create `pyproject.toml` (PEP 621), `.python-version` (`3.12`), `Makefile`; extend `.gitignore` (`.pytest_cache/`, `.coverage`, `*.egg-info/`, `.env`); `uv sync`, commit `uv.lock`; dev deps `pytest`, `pytest-cov`, `ruff`, `mypy`.
- [x] 1.3 REFACTOR - add `[tool.ruff]` (`T201` ban `print` in `src/`), `[tool.mypy]` (`strict = true`), `[tool.pytest.ini_options]`; verify `ruff`, `mypy`, `pytest` exit 0.

## Phase 2: Configuration (secure-configuration)

- [x] 2.1 RED - failing `tests/test_config.py` covering all scenarios in `specs/secure-configuration/spec.md`
- [x] 2.2 GREEN - implement `src/nora/config.py` with `Settings` per `design.md`; commit `.env.example` (placeholders only)
- [x] 2.3 REFACTOR - add `loaded_from` validator (`.env` / `process_env` / `defaults`); cover ".env.example mirrors every read variable".

## Phase 3: Sanitizer (telemetry-sanitizer)

- [x] 3.1 RED - failing `tests/test_sanitizer.py` covering all scenarios in `specs/telemetry-sanitizer/spec.md`
- [x] 3.2 GREEN - implement `src/nora/sanitizer.py` with `Sanitizer`, `SanitizedText` (text+counts), `SanitizerInputError(TypeError)`; per-instance deterministic alias map; regex covers 10/8, 172.16/12, 192.168/16, 127/8 IPv4, MAC, serial, hostname.
- [x] 3.3 REFACTOR - extract regex constants to module level; alias map absent at INFO, counter at DEBUG only.

## Phase 4: LLM Providers (llm-provider-interface)

- [x] 4.1 RED - failing `tests/test_llm.py` covering all scenarios in `specs/llm-provider-interface/spec.md`
- [x] 4.2 GREEN - implement `src/nora/llm.py`: `LLMProvider` ABC, `Completion` (frozen text/model_id/raw), `LLMUnavailable`, `UnknownProviderError`, `LMStudioProvider` (`lmstudio.Client`), `GeminiProvider` (`google.genai.Client`), `build_provider(settings)` singleton; SDK errors -> `LLMUnavailable`.
- [x] 4.3 REFACTOR - extract SDK-error map to module constant; INFO log of provider+model to stderr.

## Phase 5: MCP Server (nora-mcp-server)

- [x] 5.1 RED - failing `tests/test_server.py` covering all scenarios in `specs/nora-mcp-server/spec.md`
- [x] 5.2 GREEN - implement `src/nora/server.py` with `FastMCP("nora")`, `@mcp.tool def nora_health(...)` returning `{version, active_provider, connectivity, env_loaded}`; wire to `build_provider(settings)`; `logging.basicConfig(stream=sys.stderr)`.
- [x] 5.3 REFACTOR - add `tests/conftest.py` (tmp `.env`, fake provider); add `src/nora/__main__.py` wiring Settings -> factory -> server.

## Phase 6: Integration Tests + Final Wiring

- [x] 6.1 RED - failing `tests/test_integration.py`: spawn `python -m nora` subprocess, send `tools/list` JSON-RPC on stdin, assert `nora_health` in response + startup log on stderr.
- [x] 6.2 GREEN - make e2e pass (adjust `server.py`/`conftest.py` only; never loosen stdout/stderr contract).
- [x] 6.3 REFACTOR - final `ruff check .` + `ruff format --check .` + `mypy --strict src/nora` + `pytest` all-green; verify `git diff --stat` <= 800 lines; confirm no IPs/MACs/serials/hostnames/credentials in any artifact.

## Phase 7: Verify-Driven Remediation (post-verify FAIL)

- [x] 7.1 RED - failing test in `tests/test_llm.py` asserting `provider.complete("10.0.0.5")` invokes SDK with sanitized prompt
- [x] 7.2 GREEN - wire `_SAN.sanitize(prompt)` into both `LMStudioProvider.complete()` and `GeminiProvider.complete()` in `src/nora/llm.py`
- [x] 7.3 RED - confirm `tests/test_integration.py::test_subprocess_responds_to_tools_list_with_nora_health` is flaky (5/5 runs)
- [x] 7.4 GREEN - replace `time.sleep(0.4)` with stdout line-read loop waiting for MCP `id=0` response
- [x] 7.5 RED - confirm `tests/test_toolchain.py::test_pytest_coverage_table_for_src_nora_is_printed` is flaky (5/5 runs)
- [x] 7.6 GREEN - isolate subprocess pytest with unique `COVERAGE_FILE` (e.g., `tmp_path / f"coverage-{os.getpid()}.sqlite"`)
- [x] 7.7 FIX - edit `openspec/config.yaml` `apply.test_command` and `verify.test_command` to `uv run python -m pytest`
