# Delta for llm-provider-interface

## REMOVED Requirements

### Requirement: `LLMProvider` Abstract Base Class

(Reason: Locked decision 3 — `LLMProvider` is removed from NORA entirely; openchat UIWeb owns the LLM surface client-side. `src/nora/llm.py` is deleted.)

### Requirement: `LLMProviderFactory` Reads Configuration Once

(Reason: Locked decision 3 — `LLMProviderFactory` is removed alongside `LLMProvider`.)

### Requirement: `LMStudioProvider` Wraps the Native SDK

(Reason: Locked decision 3 — `LMStudioProvider` is removed; `lmstudio>=1,<2` dropped from `pyproject.toml`.)

### Requirement: `GeminiProvider` Wraps `google-genai`

(Reason: Locked decision 3 — `GeminiProvider` is removed; `google-genai>=1,<3` dropped from `pyproject.toml`.)

### Requirement: No Automatic Fallback Between Providers

(Reason: Locked decision 3 — no LLM providers remain inside NORA; rule is moot.)

### Requirement: Raw Responses and No Auto-Retry

(Reason: Locked decision 3 — no LLM providers remain inside NORA; rule is moot.)

### Requirement: Edge Cases

(Reason: Locked decision 3 — edge cases were tied to `LLMProvider`; moot.)

### Requirement: Security Boundary — Credentials Never Appear in Completions

(Reason: Locked decision 3 — no LLM completions remain inside NORA; rule is moot.)

### Requirement: Observability — Provider and Model Surfaced

(Reason: Locked decision 3 — no LLM providers remain inside NORA; rule is moot.)