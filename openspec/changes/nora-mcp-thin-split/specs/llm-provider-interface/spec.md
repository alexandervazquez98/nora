# Delta for llm-provider-interface (ARCHIVED)

## Purpose

The `llm-provider-interface` capability is being archived as part of `nora-mcp-thin-split`. Locked decision 3 eliminates `LLMProvider`, `LLMProviderFactory`, `LMStudioProvider`, and `GeminiProvider` from the codebase entirely; no replacement is shipped inside NORA. The corresponding requirement set is removed with no migration path inside NORA.

## REMOVED Requirements

All requirements previously declared under `openspec/specs/llm-provider-interface/spec.md` are removed.

(Reason: Locked decision 3 — openchat UIWeb owns the LLM surface client-side. NORA's only production caller was `nora_health_impl` (`src/nora/server.py:110`), which is itself removed.)
(Migration: None inside NORA. `src/nora/llm.py` is deleted; `lmstudio>=1,<2` and `google-genai>=1,<3` are dropped from `pyproject.toml:24-31`; `LLMProvider` is removed from `set_runtime_state` / `get_runtime_state` and from `__all__`. Any LLM concerns are out of NORA's scope.)
