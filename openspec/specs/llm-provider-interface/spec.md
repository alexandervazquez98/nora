# llm-provider-interface Specification

## Purpose

Defines the `LLMProvider` ABC, the `LLMProviderFactory` that constructs one provider at startup, and the concrete `LMStudioProvider` and `GeminiProvider` adapters. Vendor detail stays behind each adapter; raw SDK responses are returned; any failure surfaces as `LLM_UNAVAILABLE` — never an automatic fallback.

## Requirements

