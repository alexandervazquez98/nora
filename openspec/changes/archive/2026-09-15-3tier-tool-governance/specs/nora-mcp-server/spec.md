# Delta for nora-mcp-server

## Reconciliation

**MODIFIED** under `openspec/changes/2026-09-15-3tier-tool-governance/`.
Base spec at `openspec/specs/nora-mcp-server/spec.md`. Purely additive
— R-NEW-1..R-NEW-6 preserved. New R-NEW-7..R-NEW-10 adds the `nora hitl
mint` sub-command, the `operator_confirmed` wire field, the multi-dir
prompt-registry boot wiring, and the no-args back-compat pin.

## ADDED Requirements

### Requirement: R-NEW-7 — `nora hitl mint` Sub-Command

`src/nora/__main__.py` SHALL dispatch on `argv[1]`: `mcp` → existing
`cli.main()` boot; `hitl` → new
`nora hitl mint --operator-id <id> --ttl-seconds <n>` CLI handler that
emits a signed `HitlApprovalToken` JSON payload on stdout. Unknown
sub-commands print help to stderr AND exit non-zero. `nora hitl mint`
exits non-zero on missing/invalid arguments (`--operator-id` required,
`--ttl-seconds` optional, default
`Settings.nora_hitl_token_ttl_seconds`).

#### Scenario: `nora` with no args still boots MCP (back-compat pinned)

- GIVEN the operator runs `nora` (or `python -m nora`) without a
  sub-command
- WHEN the process starts
- THEN `mcp.run(show_banner=False)` is invoked AND the twelve-tool
  surface is reachable via JSON-RPC (deprecation-alias tests
  `test_main_alias.py` and `test_integration_boot.py:323` stay green)

#### Scenario: `nora hitl mint` emits a signed token

- GIVEN `Settings.nora_hitl_signing_key` is a non-empty `SecretStr` AND
  `Settings.nora_tool_specs_dir` is configured AND the operator runs
  `nora hitl mint --operator-id alice --ttl-seconds 900`
- WHEN the sub-command handler runs
- THEN stdout carries one JSON line containing `operator_id`,
  `issued_at`, `expires_at`, `token`, AND `signature` (non-empty)
  AND exit code is `0`

#### Scenario: `nora hitl mint` rejects invalid args

- GIVEN the operator runs `nora hitl mint` without `--operator-id`
- WHEN the sub-command handler runs
- THEN exit code is non-zero AND stderr names the missing argument
  AND no token is emitted

#### Scenario: `nora` with unknown sub-command exits non-zero with help

- GIVEN the operator runs `nora bogus`
- WHEN the dispatcher parses argv
- THEN exit code is non-zero AND stderr lists the valid sub-commands
  AND `mcp.run()` is never invoked

### Requirement: R-NEW-8 — Spectrum Tool Wire Shape Gains `operator_confirmed`

The `snmp_run_spectrum_analysis` `@mcp.tool` registration SHALL
declare `operator_confirmed: bool = False` in its FastMCP wire
schema. The runtime gate (raise `Tier1ClearanceRequired` on False) is
owned by `pmp450i-radio-tools`; this capability owns only the wire-
shape contract.

#### Scenario: tools/list declares operator_confirmed

- GIVEN the server booted via `mcp.run()` over stdio
- WHEN a client calls `tools/list` and inspects the
  `snmp_run_spectrum_analysis` entry
- THEN `inputSchema.properties.operator_confirmed.type == "boolean"`
  AND the default is `false`

### Requirement: R-NEW-9 — Boot Wires Multi-Dir Prompt Registry

`cli.main()` SHALL call `PromptRegistry.from_settings(settings)` after
`Settings()` is loaded AND before `mcp.run(show_banner=False)`. The
`from_settings` implementation reads `Settings.nora_tool_specs_dir`
and scans both `src/nora/prompts/` and `docs/tool_specs/`. Boot SHALL
NOT abort solely because the tool-spec dir is absent (see
`prompt-registry` R8).

#### Scenario: cli.main wires PromptRegistry.from_settings

- GIVEN `cli.main()` source
- WHEN it is scanned for `from_settings`
- THEN at least one `PromptRegistry.from_settings(settings)` call is
  present AND it precedes `mcp.run(show_banner=False)`

#### Scenario: registration guard still passes for all twelve tools

- GIVEN the boot path runs the existing R-NEW-6 registration guard
- WHEN the guard inspects the twelve `@mcp.tool` registrations
- THEN `UncataloguedToolError` is NOT raised AND `mcp.run()` proceeds
  (no regression from R-NEW-6)

### Requirement: R-NEW-10 — `nora-mcp` Alias Untouched

The existing `nora-mcp = "nora.cli:main"` console-script entry SHALL
remain unchanged. New functionality is exposed ONLY through the
`nora` dispatcher (Approach A from proposal §"Prompt composition
approach"). A separate `nora-hitl` console script is **rejected**
(OPEN QUESTION 3 — design must explicitly reject it with rationale).

#### Scenario: nora-mcp entry point is preserved

- GIVEN `pyproject.toml`'s `[project.scripts]` table
- WHEN the table is parsed
- THEN `nora = "nora.__main__:main"` AND `nora-mcp =
  "nora.cli:main"` are present AND no `nora-hitl` entry exists

## Open Questions (deferred to design)

- **Q1** — composition mechanism (inline marker vs runtime lookup).
  Owned by `prompt-registry`.
- **Q3** — `__main__.py` refactor and `nora-hitl` exposure. Default
  NO; design owns.
- **Q4** — tool-spec front-matter schema. Owned by `prompt-registry`.

## Cross-References

`tool-service-impact-tiers` (CLI shape driver), `hitl-approval-tokens`
(`nora hitl mint` output contract), `pmp450i-radio-tools`
(`operator_confirmed` wire field declared here, gate enforced there),
`prompt-registry` (multi-dir scan wired at boot),
`secure-configuration` (two new settings read at boot).