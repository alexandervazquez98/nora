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

### Requirement: System-Prompt Versioning

System prompts at `src/nora/prompts/*.md` MUST declare front-matter
with `version` (strict SemVer, validated via `packaging.version.Version`),
`nora_compatibility` (a `packaging.specifiers.SpecifierSet` range that
MUST contain the running `nora.__version__`), `governance` (a dict of
non-negative ints for keys `tier_0`, `tier_1`, `tier_2`), and
`checksum_sha256` (64-char lowercase hex of `sha256(body_bytes)`). The
discriminator between system-prompt and tool-spec paths is the source
directory: files under `src/nora/prompts/` use this version-based
schema; files under `docs/tool_specs/` keep the tier-based schema.
A violation raises `PromptNotFoundError` and the file is dropped from
the registry.

#### Scenario: system prompt declares full version-based front-matter

- GIVEN `src/nora/prompts/netops_orchestrator.md` has front-matter with `version: "0.3.5"`, `nora_compatibility: ">=0.3.4,<0.4.0"`, `governance: {tier_0: 8, tier_1: 1, tier_2: 3}`, `checksum_sha256: "<64-hex-of-body>"`
- WHEN `PromptRegistry.scan()` processes the file
- THEN the prompt is accepted and exposed as `get("netops_orchestrator")`

#### Scenario: system prompt with non-SemVer version is rejected

- GIVEN `src/nora/prompts/<name>.md` has `version: "v0.3.5"` (leading `v`)
- WHEN the validator runs
- THEN `packaging.version.Version` raises `InvalidVersion`
- AND the registry raises `PromptNotFoundError` with a message naming the field

#### Scenario: tool-spec with tier-based schema is NOT validated as a system prompt

- GIVEN `docs/tool_specs/snmp_get_ap_summary.md` has only `name`, `description`, `tier`
- WHEN `PromptRegistry.scan()` processes the file
- THEN the validator uses `_validate_tool_spec` (tier-based), NOT `_validate_system_prompt`
- AND the tool-spec is accepted

### Requirement: Checksum Drift Detection

The `checksum_sha256` declared in a system prompt's front-matter MUST
equal `sha256(body_bytes).hexdigest()` computed at scan time. The
registry computes the expected value from the file bytes and compares
to the declared value. A mismatch indicates the body was edited without
updating the front-matter (drift) and raises `PromptNotFoundError` with
the expected and actual values in the error message. This is the
operator-side guard against silent prompt mutations.

#### Scenario: matching checksum is accepted

- GIVEN a system prompt whose body bytes have `sha256 = X`
- AND the front-matter declares `checksum_sha256: X`
- WHEN `PromptRegistry.scan()` runs
- THEN the prompt is accepted

#### Scenario: mismatched checksum raises PromptNotFoundError

- GIVEN a system prompt whose body bytes have `sha256 = X`
- AND the front-matter declares `checksum_sha256: Y` (Y != X)
- WHEN `PromptRegistry.scan()` runs
- THEN `PromptNotFoundError` is raised
- AND the error message includes both X (expected) and Y (actual)

#### Scenario: missing checksum_sha256 raises PromptNotFoundError

- GIVEN a system prompt with front-matter that omits `checksum_sha256`
- WHEN the validator runs
- THEN `PromptNotFoundError` is raised
- AND the error message names the missing field

### Requirement: Compatibility Range Enforcement

The `nora_compatibility` front-matter field is a `packaging.specifiers.SpecifierSet`
range (e.g. `">=0.3.4,<0.4.0"`). At scan time the registry evaluates
the range against `nora.__version__` and rejects the prompt when the
range does not contain the running version. This prevents a stale
prompt (designed for an older NORA) from being loaded by a newer
NORA whose tool surface or governance rules may have diverged.

#### Scenario: satisfied range is accepted

- GIVEN `nora.__version__ == "0.3.5"` and the prompt declares `nora_compatibility: ">=0.3.4,<0.4.0"`
- WHEN the validator runs
- THEN the prompt is accepted

#### Scenario: unsatisfied range raises PromptNotFoundError

- GIVEN `nora.__version__ == "0.5.0"` and the prompt declares `nora_compatibility: ">=0.3.4,<0.4.0"`
- WHEN the validator runs
- THEN `PromptNotFoundError` is raised with `reason="nora_compatibility_unsatisfied"`
- AND the running version is in the error message

#### Scenario: malformed range raises PromptNotFoundError

- GIVEN a prompt declares `nora_compatibility: "not-a-range"`
- WHEN the validator runs
- THEN `packaging.specifiers.InvalidSpecifier` is caught
- AND `PromptNotFoundError` is raised with the malformed value in the message

### Requirement: Watermark Banner Rendering

`PromptRegistry.render(name) -> str` MUST return the prompt body with
a watermark banner prepended for system prompts:

```
<!-- NORA-PROMPT: <name> v<version> [sha: <first-8-hex>] -->

<body>
```

The SHA is the first 8 hex chars of `checksum_sha256`. For tool-specs
(without `version` + `checksum_sha256` in metadata), `render` returns
the body unchanged. All `@mcp.prompt` wrappers and the
`nora_get_tool_spec` tool body MUST switch from `registry.get(name).body`
to `registry.render(name)` so the watermark reaches the LLM at runtime.

#### Scenario: system prompt renders with watermark banner

- GIVEN `netops_orchestrator` is registered with `version: "0.3.5"` and `checksum_sha256: "bd03c97f..."`
- WHEN `registry.render("netops_orchestrator")` is called
- THEN the result starts with `<!-- NORA-PROMPT: netops_orchestrator v0.3.5 [sha: bd03c97f] -->

`
- AND the original body follows verbatim below

#### Scenario: tool-spec renders without watermark (no-op)

- GIVEN `snmp_get_ap_summary` is registered as a tool-spec (no `version` in metadata)
- WHEN `registry.render("snmp_get_ap_summary")` is called
- THEN the result equals `prompt.body` byte-for-byte (no banner prepended)

#### Scenario: SHA in banner is truncated to 8 hex chars

- GIVEN a system prompt with `checksum_sha256: "abcdef0123456789..."` (64 chars)
- WHEN `registry.render(name)` is called
- THEN the watermark contains `[sha: abcdef01]` (8 hex chars), NOT the full 64

#### Scenario: render on unknown name raises PromptNotFoundError

- GIVEN `registry.get(name)` raises `PromptNotFoundError` for an unknown name
- WHEN `registry.render(name)` is called for the same name
- THEN `PromptNotFoundError` is raised with the same message

### Requirement: Open WebUI Sync

The `nora prompt sync` CLI subcommand MUST synchronise every registered
system prompt to Open WebUI's `/api/v1/models` REST endpoint:

- For each system prompt, POST `<base>-v<X.Y.Z>` as an **immutable**
  profile. A `409 CONFLICT` response (profile already exists) is
  treated as idempotent success.
- PUT `<base>-latest` as a **mutable alias** pointing at the freshly
  synced version.

Both bodies MUST carry metadata `{commit_sha, release_tag, synced_at,
nora_version, prompt_version}`. The sync uses `OPENWEBUI_BASE_URL`
(default `http://localhost:8080`) and `OPENWEBUI_ADMIN_API_KEY` (required).
Sync is **manual only** — there is no automatic sync on boot. Auth
failures (401/403) raise `OpenWebUIAuthError`; transport failures
(5xx, connection errors) raise `OpenWebUISyncError`.

#### Scenario: sync creates versioned profile for each registered prompt

- GIVEN the registry has `netops_orchestrator` at `version: "0.3.5"`
- WHEN `nora prompt sync` runs against Open WebUI
- THEN exactly one POST to `/api/v1/models` carries body `id == "nora-netops-v0.3.5"`
- AND the body contains the rendered prompt with the watermark banner

#### Scenario: sync treats 409 CONFLICT as idempotent success

- GIVEN `nora-netops-v0.3.5` already exists in Open WebUI
- WHEN `nora prompt sync` runs
- THEN no exception is raised
- AND the result list records `action="already_exists"` for that prompt

#### Scenario: sync updates latest alias to the new version

- GIVEN the registry has `netops_orchestrator` at `version: "0.3.5"`
- AND `nora-netops:latest` currently points at `v0.3.4`
- WHEN `nora prompt sync` runs
- THEN a PUT to `/api/v1/models/nora-netops-latest` (or the equivalent update route) overwrites the alias
- AND the new body references `version: "0.3.5"` and the freshly computed watermark SHA

#### Scenario: sync raises on missing admin API key

- GIVEN `OPENWEBUI_ADMIN_API_KEY` is unset AND `--admin-api-key` is not provided
- WHEN `nora prompt sync` runs
- THEN the process exits with code 2
- AND stderr names the missing variable

#### Scenario: sync raises OpenWebUIAuthError on 401/403

- GIVEN Open WebUI returns 401 for the configured API key
- WHEN `nora prompt sync` runs
- THEN `OpenWebUIAuthError` is raised
- AND the process exits with code 1

#### Scenario: tool-specs are excluded from sync

- GIVEN the registry contains only tool-specs (no system prompts)
- WHEN `nora prompt sync` runs
- THEN no POST or PUT requests are made
- AND the result list is empty

## Resolved Decisions

- **Q1 (composition mechanism) — frozen as per-tool `@mcp.prompt`:** Each tool-spec is exposed as a separate MCP prompt whose name matches the tool name (e.g. `@mcp.prompt def snmp_reboot_radio() -> str`). The LLM resolves tool→spec at runtime via `get_prompt(name="<tool>")`. The FastMCP server keeps one thin wrapper per tool; the canonical body lives in `docs/tool_specs/<tool>.md` and is fetched through `PromptRegistry.get(...)`. *(Decided 2026-09-20 while implementing issue #72's canonical prompt source layer.)*
- **Q4 (front-matter schema) — frozen as tier-conditional mandatory:** Tool-spec front-matter MUST contain `name`, `description`, AND `tier: 0 | 1 | 2`. Additionally: `tier: 1` ⇒ `requires_operator_confirmed: true` is mandatory; `tier: 2` ⇒ `requires_hitl_token: true` is mandatory. The validator in `PromptRegistry._validate_tool_spec` enforces these cross-field invariants at boot; a violation raises `PromptNotFoundError` and the spec is dropped from the registry. *(Decided 2026-09-20 while implementing issue #72's canonical prompt source layer.)*
- **Q5 (prompt SemVer policy) — frozen as patch/minor/major with concrete meanings:** Patch = clarity, typo fixes, additional few-shot examples. Minor = tool additions, new governance rules, new operational sections. Major = structural governance shifts or breaking behavioural modifications. The SemVer signal feeds into the NORA package version bump. *(Decided 2026-09-20 while implementing issue #45.)*
- **Q6 (watermark banner format) — frozen as `<!-- NORA-PROMPT: <name> v<version> [sha: <first-8-hex>] -->
\n<body>`:** SHA truncated to 8 hex chars for human readability (not a security token — the full 64-char digest is in `metadata["checksum_sha256"]` for drift detection). The `<!-- ... -->` comment syntax survives markdown rendering in any client. The watermark applies to system prompts only; tool-specs return their body unchanged because they lack `version` + `checksum_sha256` metadata. *(Decided 2026-09-20 while implementing issue #45.)*
- **Q7 (versioned model profile naming) — frozen as `<model>-v<X.Y.Z>` immutable + `<model>-latest` mutable alias:** The versioned profile is created via POST on every sync; a `409 CONFLICT` response is idempotent (no-op). The `latest` alias is PUT-updated in place to point at the freshly synced version. Both carry `{commit_sha, release_tag, synced_at, nora_version, prompt_version}` metadata. `<model>` defaults to `nora-netops`. *(Decided 2026-09-20 while implementing issue #45.)*
- **Q8 (two-schema split for front-matter) — frozen as system-prompts version-based + tool-specs tier-based:** System prompts under `src/nora/prompts/*.md` use the version-based schema (name + description + version + nora_compatibility + governance + checksum_sha256). Tool specs under `docs/tool_specs/*.md` keep the tier-based schema (name + description + tier + tier-conditional required flags). Discrimination is by directory, not by file content. One registry, two validators. *(Decided 2026-09-20 while implementing issue #45.)*

## Cross-References

- `secure-configuration` — `prompts_dir` follows the additive `Settings` pattern; env-var surface inherits the locked-no-hardcoded-values rule.
- `telemetry-sanitizer` — the registry MUST NOT mutate prompt bodies through the sanitizer.
- `driver-snmp-pmp450i` — the registered prompt is the system context used when the LLM plans a driver call.
