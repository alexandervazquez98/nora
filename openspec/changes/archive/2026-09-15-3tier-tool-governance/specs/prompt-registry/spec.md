# Delta for prompt-registry

## Reconciliation

**MODIFIED** under `openspec/changes/2026-09-15-3tier-tool-governance/`.
Base spec at `openspec/specs/prompt-registry/spec.md`. Purely additive
— R1–R7 preserved. New R8 contract extends `PromptRegistry.scan(...)`
to accept multiple source dirs so tool specs under
`Settings.nora_tool_specs_dir` load alongside packaged prompts.

## ADDED Requirements

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

### Requirement: Tool-Spec Front-Matter Schema (Frozen Subset)

Tool-spec files (`docs/tool_specs/*.md`) SHALL carry YAML front-matter
with at minimum `name`, `description`, AND `tier: 0 | 1 | 2`. The full
schema (whether `requires_operator_confirmed` and
`requires_hitl_token` are mandatory) is **OPEN QUESTION 4** for design
phase. Until design freezes the schema, only `tier` is contract-
bearing; extended fields MAY appear but are not yet validated.

#### Scenario: every tool-spec file declares tier

- GIVEN each of the eleven `docs/tool_specs/<tool_name>.md` files
- WHEN front-matter is parsed
- THEN `tier` is present AND its value is one of `0`, `1`, `2`

#### Scenario: README.md has no tier marker

- GIVEN `docs/tool_specs/README.md`
- WHEN front-matter is parsed
- THEN `tier` is absent (the README is not a tool)

## Open Questions (deferred to design)

- **Q1 (composition mechanism):** inline `<!-- tool: name -->` marker
  resolved at scan time, vs runtime name-lookup at `@mcp.prompt` call
  time. Spec does not choose; design owns.
- **Q4 (front-matter schema):** whether `requires_operator_confirmed`
  and `requires_hitl_token` are mandatory on tool-spec front-matter.
  Schema must freeze BEFORE `docs/tool_specs/*.md` files are authored.

## Cross-References

`tool-service-impact-tiers` (canonical mapping), `secure-configuration`
(`nora_tool_specs_dir` setting), `nora-mcp-server` (boot wires
`from_settings` with both dirs).