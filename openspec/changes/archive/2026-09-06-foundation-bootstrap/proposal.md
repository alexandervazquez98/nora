# Proposal: Toolchain Foundation for NORA (Phase 1)

> Phase 1 — SCOPE.md §6.

## Intent

NORA is greenfield (only `LICENSE` + `SCOPE.md` tracked). System Python 3.9.6 cannot run modern NORA deps (fastmcp, google-genai, pydantic-settings all ≥3.10); no manifest, venv, coverage, or type checker exist. SCOPE §3 Phase 1 mandates a secure config pipeline, telemetry sanitizer, and multi-LLM connector. This change delivers that foundation in one reviewable unit.

## Scope

**In**: Python 3.12 via `uv`; `pyproject.toml` PEP 621, `uv.lock`, `.python-version`; `ruff` + strict-TDD `pytest` + `pytest-cov` + `mypy --strict`; `src/nora/` + `tests/`; Pydantic Settings + synthetic `.env.example`; sanitizer (fixed mask of private IPs, MACs, serials, hostnames → aliases); `LLMProvider` ABC + factory + `LMStudioProvider` + `GeminiProvider` (raw responses); FastMCP boot + `nora_health`.

**Out**: driver layer (Phase 2), HITL `ChangeRequest` (Phase 3), more MCP tools, in-process provider switching, auto-fallback.

## Capabilities

> Contract for `sdd-spec`. Each becomes `openspec/specs/<name>/spec.md`.

### New Capabilities

- **`project-toolchain`**: build, lint, format, test, type-check, coverage pinned via `uv` + `pyproject.toml`.
- **`secure-configuration`**: Pydantic `Settings`; synthetic `.env.example`; never reads `os.environ` inline.
- **`telemetry-sanitizer`**: fixed mask of private IPs, MACs, serials, hostnames → aliases; runs before any external call.
- **`llm-provider-interface`**: `LLMProvider` ABC + factory; LMStudio via the **native `lmstudio` SDK** (`pip install lmstudio`, not the OpenAI-compatible REST endpoint) — gives typed errors (`LMStudioError`/`LMStudioTimeoutError`/`LMStudioPredictionError`/`LMStudioClientError`), model loading, streaming, and chat helpers. Gemini via **`google-genai<3.0.0`** (`pip install google-genai`, import `from google import genai`); pinned because v3 will break Automatic Function Calling. Raw responses returned; failure → `LLM_UNAVAILABLE` (no auto-fallback).
- **`nora-mcp-server`**: FastMCP boot (`fastmcp>=3.2,<4` — v3 is the stable major; v4 ships breaking changes to background tasks); stdio transport by default (Claude Desktop uses JSON-RPC over stdio); `nora_health` returns version, active provider, connectivity, `.env` load status. **Logging must go to stderr only** — stdout is reserved for the MCP protocol; logging to stdout breaks the JSON-RPC stream.

### Modified Capabilities

None.

## Approach

`uv` bootstraps Python 3.12 + venv. `LLMProvider` ABC has one method:

```python
def complete(prompt: str, *, system: str | None = None, context: dict | None = None) -> Completion
# Completion = { text: str, model_id: str, raw: Any }
```

`LLMProviderFactory` reads Pydantic Settings (`NORA_LLM_PROVIDER`), constructs exactly **one** provider at startup, holds it for the process lifetime. `LMStudioProvider` wraps `lmstudio.Client` (sync, context-managed). `GeminiProvider` wraps `google.genai.Client` (sync, context-managed). Errors from either SDK → `LLM_UNAVAILABLE` to the caller. Provider switch = `.env` edit + restart (logged). `nora_health` is read-only — calls `provider.complete(...)` with a fixed system prompt and asserts connectivity.

## Affected Areas

| Area | Impact | Description |
|------|--------|-------------|
| `pyproject.toml`, `uv.lock`, `.python-version`, `.env.example`, `Makefile` | New | Toolchain. |
| `src/nora/{config,sanitizer,llm,server}.py` | New | Phase 1 modules. |
| `tests/` | New | Mirror layout; strict TDD. |
| `.gitignore` | Modified | Exclude `.venv/`, `__pycache__/`, `.pytest_cache/`, `.coverage`. |

## Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| `LLMProvider` shape may evolve (Gemini multimodal). | Med | Tiny ABC; revisit in change #2. |
| `uv` not preinstalled. | Low | `brew install uv` once; documented in `Makefile`. |
| Sanitizer may miss obfuscated credentials. | Med | Covers IPs/MACs/serials/hostnames per SCOPE §2; extend in Phase 2. |
| ~700 LOC total. | Med | `delivery_strategy: ask-on-risk`; orchestrator stops after this phase. |
| **SDK churn across all three deps**: `google-genai` v3 (breaking AFC change), FastMCP v4 (background tasks removed), LM Studio SDK (frequent minor additions). | Med-High | Pin exact minor versions in `pyproject.toml`; `uv.lock` is authoritative; integration tests assert the **minimum** surface we depend on so a minor bump cannot silently break us. |
| **FastMCP stdout contamination**: any `print` or stdout-bound logger corrupts the JSON-RPC stream and the operator sees garbled responses. | Med | All logging goes to stderr via Python `logging` with a stderr handler; no `print()` anywhere in `src/nora/`; lint rule bans `print` in `src/`. |

## Rollback Plan

Rollback = delete `pyproject.toml`, `uv.lock`, `.python-version`, `Makefile`, `src/`, `tests/`, revert `.gitignore`. Repo returns to `LICENSE` + `SCOPE.md` + this proposal. No `.env` to scrub; provider credentials never entered code.

## Dependencies

| | |
|---|---|
| `uv` (Astral) | Python 3.12 + lockfile |
| `fastmcp>=3.2,<4` | MCP server (v3 stable; v4 in flight) |
| `lmstudio` (latest 1.x) | Native LM Studio Python SDK |
| `google-genai>=1,<3` | Gemini SDK (pin **below 3.0.0** per official warning — v3 breaks AFC) |
| `pydantic-settings` | Configuration (per SCOPE §3) |
| `mypy` (dev) | Strict type checking |
| LM Studio local server **or** `GEMINI_API_KEY` | Only the active provider is required at runtime |

No provider credentials enter the codebase. Provider selection via `NORA_LLM_PROVIDER` in `.env`.

## Success Criteria

- [ ] `uv sync` reproduces env; `python3 -m pytest` runs.
- [ ] `ruff check .` + `ruff format --check .` exit 0; `mypy --strict src/nora` passes.
- [ ] `nora_health` returns version, active provider, connectivity, `.env` load status.
- [ ] Sanitizer tests prove masking of IP, MAC, serial, hostname → alias.
- [ ] `NORA_LLM_PROVIDER` switch needs only `.env` edit + restart (logged).
- [ ] Diff ≤ 800 lines; no IPs, MACs, serials, hostnames, credentials in any artifact.