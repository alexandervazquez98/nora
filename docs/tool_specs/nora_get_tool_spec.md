---
name: nora_get_tool_spec
description: Retrieve the canonical Markdown specification body for a NORA MCP tool by name. Used by the orchestrator (see netops_orchestrator.md §7) to resolve tool → spec on demand before invocation.
tier: 0
---

# nora_get_tool_spec

## Tier

**Tier 0** — Passive Telemetry (Read-Only). Pure registry lookup; no SNMP frames, no state mutation, no operator gate.

## Operational impact

Returns the authoritative Markdown body of the named NORA MCP tool-spec — the same body the LLM would receive through the MCP `prompts/get` interface. The orchestrator prompt (netops_orchestrator.md §7) directs the LLM to call this tool BEFORE invoking any other `@mcp.tool`, so the LLM reads the precise parameter contract, tier classification, and governance protocol that apply to the target tool.

Because this tool is exposed under the `@mcp.tool` interface (not just `@mcp.prompt`), the LLM can call it from any MCP client that surfaces the tool-calling schema — including clients that do NOT bridge MCP `prompts/get` into callable tools. Clients that DO expose `prompts/get` to the model continue to work unchanged.

## Parameter contract

- `name: str` (required) — the tool name. MUST equal the basename (sans `.md`) of a file under `docs/tool_specs/` OR one of the two packaged prompts (`netops_orchestrator`, `snmp_pmp450i`).

Returns:
- `str` — the full Markdown body of the spec (front-matter stripped), same as `PromptRegistry.get(name).body`.

## Failure modes

- Unknown name → typed `PromptNotFoundError` raised at the registry layer. The FastMCP tool wrapper surfaces it as a tool error; the LLM MUST abort the planned invocation and report the error to the operator.
- Empty registry (boot failure) → `RuntimeError` from `get_prompt_registry()`; same abort rule.

## Cross-references

- `prompt-registry` spec, *Per-Tool MCP Prompt Exposure* requirement (the prompt counterpart of this bridge).
- `prompt-registry` spec, *Tool-Bridged Spec Lookup* requirement (this tool).
- `netops_orchestrator.md` §7 — Spec Lookup Protocol: this tool is what the LLM actually invokes.
