# llm-provider-interface Specification

## Purpose

Defines the `LLMProvider` ABC, the `LLMProviderFactory` that constructs one provider at startup, and the concrete `LMStudioProvider` and `GeminiProvider` adapters. Vendor detail stays behind each adapter; raw SDK responses are returned; any failure surfaces as `LLM_UNAVAILABLE` — never an automatic fallback.

## Requirements

### Requirement: `LLMProvider` Abstract Base Class

The `LLMProvider` ABC MUST declare exactly one method, `complete(prompt, *, system=None, context=None) -> Completion`, returning a `Completion` value with `text`, `model_id`, and `raw` fields. The base class MUST NOT expose vendor types in its signature.

#### Scenario: exactly one abstract method exists

- GIVEN the `LLMProvider` base class
- WHEN public methods are listed
- THEN `complete` is the only abstract method
- AND no vendor type appears in the signature

### Requirement: `LLMProviderFactory` Reads Configuration Once

The factory MUST read `NORA_LLM_PROVIDER` from `Settings`, construct one provider at startup, raise a typed error on unknown values, and MUST NOT allow in-process switching.

#### Scenario: factory constructs the named provider once

- GIVEN `NORA_LLM_PROVIDER=lmstudio`
- WHEN the factory is called twice
- THEN the SAME instance is returned

#### Scenario: unknown provider fails fast

- GIVEN `NORA_LLM_PROVIDER=openai`
- WHEN the factory runs
- THEN `UnknownProviderError` is raised and no provider is constructed

#### Scenario: provider switch requires restart

- GIVEN the process runs with `lmstudio`
- WHEN `.env` is edited to `gemini` without restarting
- THEN the running process keeps `lmstudio`

### Requirement: `LMStudioProvider` Wraps the Native SDK

`LMStudioProvider` MUST use the native `lmstudio` SDK (`import lmstudio as lms`), MUST establish the client via `lms.Client(api_host=...)` as a context manager, and MUST map SDK errors to `LLM_UNAVAILABLE`.

#### Scenario: native SDK is used (not the OpenAI REST endpoint)

- GIVEN the LMStudioProvider module
- WHEN its imports are scanned
- THEN `import lmstudio as lms` is present
- AND no HTTP call to a `v1/chat/completions` URL is made

#### Scenario: completion returns raw SDK response

- GIVEN the SDK returns a response with `.content`
- WHEN `complete("ping")` is called
- THEN `Completion.text` equals `.content`
- AND `Completion.raw` is the verbatim SDK object

#### Scenario: SDK errors map to `LLM_UNAVAILABLE`

- GIVEN the SDK raises any typed error
- WHEN `complete(...)` is called
- THEN the caller receives `LLM_UNAVAILABLE`

### Requirement: `GeminiProvider` Wraps `google-genai`

`GeminiProvider` MUST use `google-genai>=1,<3` (v3 breaks AFC), MUST establish the client via `genai.Client(api_key=...)` as a context manager, and MUST read the API key from `Settings`.

#### Scenario: pinned SDK version is used

- GIVEN `pyproject.toml`
- WHEN its dependencies are listed
- THEN `google-genai` is pinned to `>=1,<3`

#### Scenario: completion returns raw SDK response

- GIVEN the SDK returns a response with `.text`
- WHEN `complete("ping")` is called
- THEN `Completion.text` equals `.text`
- AND `Completion.raw` is the verbatim SDK object

#### Scenario: API key comes from Settings

- GIVEN the active provider is `gemini`
- WHEN `GeminiProvider.__init__` runs
- THEN the key comes from `Settings().gemini_api_key`
- AND `os.environ.get` is NOT called inside the provider module

### Requirement: No Automatic Fallback Between Providers

A provider failure MUST return `LLM_UNAVAILABLE`; the system MUST NOT silently retry with the other provider.

#### Scenario: active-provider failure returns `LLM_UNAVAILABLE`

- GIVEN the active provider's upstream is unreachable
- WHEN `complete(...)` is called
- THEN the result is `LLM_UNAVAILABLE`
- AND no call to the other provider is made

### Requirement: Raw Responses and No Auto-Retry

The system MUST return provider responses verbatim — no validation, no trimming, no schema enforcement, no auto-retry.

#### Scenario: response text is returned verbatim

- GIVEN the SDK returns text with trailing whitespace
- WHEN `complete(...)` succeeds
- THEN `Completion.text` equals the SDK text byte-for-byte

#### Scenario: no auto-retry on transient errors

- GIVEN the SDK raises a transient error once
- WHEN `complete(...)` is called
- THEN the caller receives `LLM_UNAVAILABLE` after the first attempt

### Requirement: Edge Cases

Empty prompts and prompts exceeding the model's context window MUST behave safely.

#### Scenario: empty prompt returns empty completion

- GIVEN the prompt is `""`
- WHEN `complete("")` is called
- THEN a `Completion` with `text == ""` is returned

#### Scenario: over-context prompt is surfaced

- GIVEN a prompt larger than the model's context window
- WHEN `complete(...)` is called
- THEN the SDK error maps to `LLM_UNAVAILABLE`

### Requirement: Security Boundary — Credentials Never Appear in Completions

`Completion.raw` and `Completion.text` MUST NOT contain any secret read from `Settings`.

#### Scenario: completion never echoes any secret

- GIVEN a successful provider call
- WHEN the caller inspects `Completion.text` and `Completion.raw`
- THEN no secret value is present

### Requirement: Observability — Provider and Model Surfaced

Every `Completion` MUST expose `model_id`; the factory MUST log the active provider at startup.

#### Scenario: completion carries the model id

- GIVEN any successful completion
- WHEN the caller inspects the result
- THEN `Completion.model_id` is non-empty

#### Scenario: startup logs the active provider

- GIVEN the factory is called
- WHEN the process starts
- THEN a log line at `INFO` names the active provider and model
- AND the line goes to stderr
