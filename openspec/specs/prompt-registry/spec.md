# prompt-registry Specification

## Purpose

Defines how NORA loads system prompts at boot. The `PromptRegistry` reads Markdown files from package data (or an operator override directory), validates a front-matter contract, and exposes a stable per-session lookup. Hot-reload is forbidden, a missing prompt at boot is fatal. The FastMCP server exposes every registered prompt via `@mcp.prompt`, so the LLM can resolve tool→spec on demand (see the *Per-Tool MCP Prompt Exposure* requirement below). The first shipped prompt, `snmp_pmp450i.md`, instructs the LLM about the typed `RadioMetricsReport` schema and zero-leakage rules.

## Requirements

### Requirement: One-Shot Boot Load

The `PromptRegistry` MUST load all prompts exactly once at boot. After boot, the loaded set MUST be immutable for the lifetime of the process. Hot-reload and inotify watches MUST NOT exist.

#### Scenario: registry loads once and never reloads

- GIVEN a running process whose registry has already loaded
- WHEN the operator writes a new prompt file into the prompts directory
- THEN the registry does not pick up the change

### Requirement: Default Source — Package Data

By default, the registry MUST read Markdown files from `src/nora/prompts/*.md` shipped with the wheel. Each `.md` MUST declare front-matter with `name` and `description` fields.

#### Scenario: packaged prompt loads on boot

- GIVEN `src/nora/prompts/snmp_pmp450i.md` exists with valid front-matter
- WHEN boot instantiates the registry
- THEN `registry.get("snmp_pmp450i")` returns the parsed body and `description` is preserved

### Requirement: Operator Override — `NORA_PROMPTS_DIR`

An operator MAY override the source directory via `Settings.prompts_dir` (no `os.environ` calls inline). The override MUST be honoured at boot and MUST take precedence over packaged data.

#### Scenario: override directory wins over package data

- GIVEN `Settings.prompts_dir == "/etc/nora/prompts"` containing a `snmp_pmp450i.md` whose body says "OVERRIDDEN"
- WHEN boot instantiates the registry
- THEN `registry.get("snmp_pmp450i")` returns the override body

### Requirement: Fail-Closed on Missing Prompt

A prompt referenced at boot that cannot be located MUST raise a typed `PromptNotFoundError`. The registry MUST NOT substitute a default or empty string.

#### Scenario: missing prompt aborts boot

- GIVEN `Settings.prompts_dir == "."`, no packaged copy exists, and `.` contains no `snmp_pmp450i.md`
- WHEN boot instantiates the registry
- THEN a typed `PromptNotFoundError` is raised
- AND the server does not start

### Requirement: Front-Matter Schema Validation

Every prompt file MUST begin with YAML front-matter. The `name` field MUST match the file's basename (sans `.md`); the `description` field MUST be a non-empty string. Validation failures raise `PromptNotFoundError`.

#### Scenario: a file without front-matter is rejected

- GIVEN a `foo.md` with no front-matter block
- WHEN boot validates it
- THEN a typed `PromptNotFoundError` is raised
- AND the file is NOT registered

#### Scenario: an empty description is rejected

- GIVEN a `foo.md` whose front-matter has `description: ""`
- WHEN boot validates it
- THEN a typed `PromptNotFoundError` is raised

### Requirement: Stable Per-Session Reference

The registry MUST return equal content for the same name within a single process. The second `registry.get(name)` call MUST NOT trigger I/O.

#### Scenario: identical name returns identical content without I/O

- GIVEN an active process
- WHEN `registry.get("snmp_pmp450i")` is called twice
- THEN both returned bodies compare equal
- AND no I/O is performed on the second call

### Requirement: `snmp_pmp450i.md` System Prompt

The shipped `snmp_pmp450i.md` MUST describe the `snmp_get_pmp450i_radio_metrics` tool, document the typed `RadioMetricsReport` schema (no `dict` or `Any`), and reinforce zero-leakage rules: no real IPs, MACs, serials, hostnames, or credentials in any LLM-visible output.

#### Scenario: shipped prompt declares tool name and typed schema

- GIVEN `src/nora/prompts/snmp_pmp450i.md`
- WHEN a content scan looks for the tool name and the phrase "RadioMetricsReport"
- THEN both are present
- AND the file contains no private IPv4 literal, MAC literal, or hostname literal

### Requirement: R8 — Multi-Directory Scan (Approach A)

`PromptRegistry.scan(sources)` SHALL accept one or more directories
(additive — single-dir calls stay valid). `from_settings(settings)`
SHALL build the source list from `[PACKAGED_PROMPTS_DIR,
settings.nora_tool_specs_dir]` (the tool-spec dir is skipped when
absent or `None`). Each scanned file SHALL validate against the same
front-matter contract as R5, plus an **additive** `tier: 0 | 1 | 2`
field on files discovered under `nora_tool_specs_dir`. Tool specs whose
`tier` value is invalid SHALL raise `PromptNotFoundError` (fail-closed).

(Previously: `PromptRegistry.scan(source: Path)` accepted exactly one
directory; tool specs did not exist.)

#### Scenario: both source dirs are scanned at boot

- GIVEN `src/nora/prompts/netops_orchestrator.md` AND
  `docs/tool_specs/snmp_run_spectrum_analysis.md` both exist with
  valid front-matter
- WHEN `from_settings(settings).get("netops_orchestrator")` runs AND
  `from_settings(settings).get("snmp_run_spectrum_analysis")` runs
- THEN both return their respective bodies AND both descriptions are
  preserved

#### Scenario: tool specs with tier: 1 and tier: 2 load successfully

- GIVEN `docs/tool_specs/snmp_run_spectrum_analysis.md` has
  `tier: 1` AND `docs/tool_specs/snmp_migrate_radio_frequency.md` has
  `tier: 2`
- WHEN `from_settings(settings).get(<name>)` runs for each
- THEN each returns its parsed body AND `metadata["tier"] in {0, 1, 2}`

#### Scenario: missing nora_tool_specs_dir is skipped, not fatal

- GIVEN `Settings.nora_tool_specs_dir is None` (operator override is
  off)
- WHEN `from_settings(settings)` runs
- THEN `scan(...)` is called with only `[PACKAGED_PROMPTS_DIR]` AND
  no error is raised

#### Scenario: invalid tier marker raises PromptNotFoundError

- GIVEN `docs/tool_specs/foo.md` has `tier: 9` (not in `{0, 1, 2}`)
- WHEN boot validates the file
- THEN `PromptNotFoundError` is raised AND `foo` is NOT registered

### Requirement: Orchestrator Prompt Body Augmentation

The shipped `src/nora/prompts/netops_orchestrator.md` SHALL contain a
§"Universal Service Impact & Disruption Gate" section that references
each tier (0 / 1 / 2) and names the operator-clearance protocol for
Tier 1 (`operator_confirmed=True`) and the HITL token requirement for
Tier 2. The registry SHALL compose the orchestrator prompt body at
boot to reference each tool's `tier` value from the tool-spec front-
matter; the LLM resolves tool→spec via the composed prompt.

#### Scenario: orchestrator body references every tier

- GIVEN the augmented `netops_orchestrator.md`
- WHEN a content scan looks for the phrases `Tier 0`, `Tier 1`, and
  `Tier 2`
- THEN all three appear AND the section title "Universal Service
  Impact & Disruption Gate" appears

#### Scenario: orchestrator body names the clearance and HITL protocols

- GIVEN the augmented `netops_orchestrator.md`
- WHEN a content scan looks for the phrases
  `operator_confirmed=True` AND `HITL approval token`
- THEN both appear AND the Tier-1 protocol precedes the Tier-2
  protocol

### Requirement: Tool-Spec Front-Matter Schema (Frozen)

Tool-spec files (`docs/tool_specs/*.md`) SHALL carry YAML front-matter
with exactly:

- `name: str` — MUST equal the file basename (sans `.md`).
- `description: str` — MUST be a non-empty single-line summary.
- `tier: 0 | 1 | 2` — the impact tier per `tool-service-impact-tiers`.

Cross-field conditional requirements (mandatory when the corresponding
tier is declared):

- `tier: 1` ⇒ `requires_operator_confirmed: true` is mandatory.
- `tier: 2` ⇒ `requires_hitl_token: true` is mandatory.

Validation failures raise `PromptNotFoundError` and the offending spec
is NOT registered. `name` matching the basename and `description`
non-empty remain unconditional. The tier-conditional invariants are
enforced by `PromptRegistry._validate_tool_spec` at boot time.

#### Scenario: every tool-spec file declares tier

- GIVEN each `docs/tool_specs/<tool_name>.md` file shipped with the project
- WHEN front-matter is parsed
- THEN `tier` is present AND its value is one of `0`, `1`, `2`

#### Scenario: tier-1 tool-spec declares requires_operator_confirmed

- GIVEN a tool-spec file declaring `tier: 1`
- WHEN front-matter is parsed
- THEN `requires_operator_confirmed == true` is present

#### Scenario: tier-2 tool-spec declares requires_hitl_token

- GIVEN a tool-spec file declaring `tier: 2`
- WHEN front-matter is parsed
- THEN `requires_hitl_token == true` is present

#### Scenario: tier-1 tool-spec without requires_operator_confirmed is rejected

- GIVEN a `docs/tool_specs/foo.md` declaring `tier: 1` but no `requires_operator_confirmed`
- WHEN boot validates it
- THEN a typed `PromptNotFoundError` is raised AND `foo` is NOT registered

#### Scenario: tier-2 tool-spec without requires_hitl_token is rejected

- GIVEN a `docs/tool_specs/foo.md` declaring `tier: 2` but no `requires_hitl_token`
- WHEN boot validates it
- THEN a typed `PromptNotFoundError` is raised AND `foo` is NOT registered

#### Scenario: README.md has no tier marker

- GIVEN `docs/tool_specs/README.md`
- WHEN front-matter is parsed
- THEN `tier` is absent (the README is not a tool)

### Requirement: Per-Tool MCP Prompt Exposure

The FastMCP server MUST expose every tool-spec under
`docs/tool_specs/*.md` as an `@mcp.prompt` whose name equals the tool
name. The wrapper function MUST return
`PromptRegistry.get(name).body` (i.e., the canonical Markdown body
shipped in `docs/tool_specs/`). The shipped orchestrator prompt
(`netops_orchestrator`) and the historical `snmp_pmp450i` prompt are
also exposed under the same mechanism for symmetry. Boot MUST fail
fast (typed `PromptNotFoundError`) if any registered tool-spec is not
exposed via `@mcp.prompt`.

#### Scenario: every tool-spec is reachable via get_prompt

- GIVEN all 13 tool-spec files are present in `docs/tool_specs/` with valid front-matter
- WHEN the FastMCP server is booted
- THEN `prompts/list` returns 13 entries whose names match the tool-spec basenames
- AND each `get_prompt(name=<tool>)` returns the body of the matching `docs/tool_specs/<tool>.md`

#### Scenario: netops_orchestrator prompt is reachable

- GIVEN `src/nora/prompts/netops_orchestrator.md` is shipped
- WHEN the FastMCP server is booted
- THEN `get_prompt(name="netops_orchestrator")` returns the orchestrator body

#### Scenario: tool-spec body equals registry body

- GIVEN any `docs/tool_specs/<tool>.md` registered successfully
- WHEN `get_prompt(name=<tool>)` is called via MCP
- THEN the returned body equals `PromptRegistry.get(<tool>).body` byte-for-byte

#### Scenario: boot fails when a tool-spec is not exposed via @mcp.prompt

- GIVEN a tool-spec file `docs/tool_specs/<tool>.md` exists and is registered
- AND no `@mcp.prompt` named `<tool>` is registered on the FastMCP server
- WHEN the server is booted
- THEN a typed `PromptNotFoundError` is raised AND the server does not start

### Requirement: Tool-Bridged Spec Lookup

The FastMCP server MUST expose a Tier-0 `@mcp.tool` named
`nora_get_tool_spec(name: str) -> str` whose body returns
`PromptRegistry.get(name).body`. The bridge tool MUST be reachable
via `tools/list` AND `tools/call` from any MCP client (the standard
tool-calling schema the LLM sees during a turn), so that the LLM can
resolve tool → spec on demand without relying on the host client to
bridge MCP `prompts/get` into callable tools. The bridge tool is
exempt from the OID-catalog envelope and the `_EXPECTED_TOOL_TIERS`
taxonomy because it consumes the in-memory registry rather than
acting as a wire-affecting operator; its `_ALLOWED_UNCATALOGUED_TOOLS`
entry documents this exemption.

#### Scenario: bridge tool is registered on the FastMCP instance

- GIVEN the FastMCP server is booted with the shipped tool surface
- WHEN `mcp.list_tools()` is awaited
- THEN the set of names includes `nora_get_tool_spec`

#### Scenario: bridge tool returns the registry body byte-for-byte

- GIVEN `PromptRegistry` is initialised with the shipped prompts + tool-specs
- WHEN `nora_get_tool_spec(name="<tool>")` is invoked via `tools/call`
- THEN the returned string equals `PromptRegistry.get(<tool>).body`

#### Scenario: bridge tool surfaces PromptNotFoundError on unknown names

- GIVEN `nora_get_tool_spec` is registered
- WHEN it is invoked with `name="no_such_tool"`
- THEN the FastMCP tool error envelope references the unknown name
  AND the registry layer's `PromptNotFoundError` (or wrapped equivalent)
  is reachable from the exception chain

#### Scenario: bridge tool is Tier 0

- GIVEN `docs/tool_specs/nora_get_tool_spec.md` declares `tier: 0`
- WHEN boot validates the spec
- THEN the validator accepts it (tier 0 implies neither
  `requires_operator_confirmed` nor `requires_hitl_token`)

#### Scenario: orchestrator §7 directs the LLM to the bridge tool

- GIVEN the shipped `netops_orchestrator.md` body
- WHEN a content scan looks for the pattern `nora_get_tool_spec(name=`
- THEN the pattern appears at least once

## Resolved Decisions

- **Q1 (composition mechanism) — frozen as per-tool `@mcp.prompt`:** Each tool-spec is exposed as a separate MCP prompt whose name matches the tool name (e.g. `@mcp.prompt def snmp_reboot_radio() -> str`). The LLM resolves tool→spec at runtime via `get_prompt(name="<tool>")`. The FastMCP server keeps one thin wrapper per tool; the canonical body lives in `docs/tool_specs/<tool>.md` and is fetched through `PromptRegistry.get(...)`. *(Decided 2026-09-20 while implementing issue #72's canonical prompt source layer.)*
- **Q4 (front-matter schema) — frozen as tier-conditional mandatory:** Tool-spec front-matter MUST contain `name`, `description`, AND `tier: 0 | 1 | 2`. Additionally: `tier: 1` ⇒ `requires_operator_confirmed: true` is mandatory; `tier: 2` ⇒ `requires_hitl_token: true` is mandatory. The validator in `PromptRegistry._validate_tool_spec` enforces these cross-field invariants at boot; a violation raises `PromptNotFoundError` and the spec is dropped from the registry. *(Decided 2026-09-20 while implementing issue #72's canonical prompt source layer.)*

## Cross-References

- `secure-configuration` — `prompts_dir` follows the additive `Settings` pattern; env-var surface inherits the locked-no-hardcoded-values rule.
- `telemetry-sanitizer` — the registry MUST NOT mutate prompt bodies through the sanitizer.
- `driver-snmp-pmp450i` — the registered prompt is the system context used when the LLM plans a driver call.
