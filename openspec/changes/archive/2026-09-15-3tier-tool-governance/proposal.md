# Proposal: 3-Tier Tool Service-Impact Governance for NORA MCP (issue #43)

**Status:** Proposed (draft) — 2026-09-15
**Change:** `2026-09-15-3tier-tool-governance`
**Repo:** `alexandervazquez98/nora`
**Issue:** #43 — RFC by repo owner; scope B+ confirmed.
**Roadmap phase (SCOPE.md):** Phase 2 (driver/tooling) for the Tier-1 / Tier-2 enforcement surfaces; Phase 3 (HITL ChangeRequest cluster) absorbs the cryptographic token lifecycle. This change closes the Phase-3 stub and ships Phase-2 server-side guards; no new Phase-3 surface is opened.

---

## Why

In NOC operations, an LLM orchestrator that invokes a tool autonomously can disrupt live service. Cambium PMP 450i makes this concrete: `snmp_run_spectrum_analysis` is wired as a passive SNMP GET in NORA, but on the real radio it drops sector transmission and disassociates subscriber modules. Issue #43 proposes three coupled mitigations — (1) classify every NORA MCP tool into a 3-tier Service Impact taxonomy, (2) move per-tool operational detail out of the orchestrator prompt and into `docs/tool_specs/<tool>.md`, and (3) add a **Universal Service Impact & Disruption Gate** to the orchestrator prompt plus deterministic **server-side enforcement** so the LLM cannot bypass the gate by claiming `operator_confirmed=True` without operator clearance, and cannot mint a Tier-2 HITL token because the existing `verify_approval_token` is a JSON-shape-only stub — code reading at `src/nora/hitl/tokens.py:107-157` shows no signature field, no HMAC, no comparison digest. This change closes both gaps and ships the operator-facing `nora hitl mint` CLI the RFC requires.

## What changes (6 deliverables)

1. **3-tier classification** — Tier 0 / Tier 1 / Tier 2 mapped to 11 tools (see §Tier Classification). Enforced server-side, not just in prose.
2. **Modular tool specs** — 12 files under `docs/tool_specs/` (11 tools + README), English, YAML front-matter contract TBD by design.
3. **Orchestrator prompt refactor** — `src/nora/prompts/netops_orchestrator.md` gains a §6 "Universal Service Impact & Disruption Gate" referencing each tool tier.
4. **Tier 1 server-side lock** — `@mcp.tool snmp_run_spectrum_analysis` gains `operator_confirmed: bool = False`; False (or absent) raises a typed `DriverError` subclass (e.g. `Tier1ClearanceRequired`) **before any SNMP GET** is emitted. Mirrors the `MaintenanceWindowViolation` pattern at `src/nora/drivers/snmp_pmp450i/spectrum.py`.
5. **Tier 2 CLI + HMAC** — `nora hitl mint --operator-id <id> --ttl-seconds 900` sub-command. `mint_token` now produces HMAC-SHA256 signature; `verify_approval_token` recomputes and `hmac.compare_digest`s. Legacy stub tokens FAIL verification with a clear error (no silent acceptance). CLI dispatcher preserves `nora` no-args → MCP boot (deprecation-alias tests pin this).
6. **Prompt composition** — `PromptRegistry` extended (Approach A) to scan both `src/nora/prompts/*.md` AND `Settings.nora_tool_specs_dir` (default `docs/tool_specs/`); the orchestrator prompt body is augmented at boot. No new module.

## Impact

| Area | Impact | Description |
|------|--------|-------------|
| `openspec/specs/pmp450i-radio-tools/` | Modified | Spectrum slice gains `operator_confirmed` prerequisite. Migration slice is unchanged (gate already in place). |
| `openspec/specs/prompt-registry/` | Modified | Additive R8: multi-dir scan (packaged prompts + tool specs). |
| `openspec/specs/nora-mcp-server/` | Modified | New tier-1 tool parameter, new `nora hitl mint` sub-command, new tool-spec surface. |
| `openspec/specs/secure-configuration/` | Modified | New settings `nora_hitl_signing_key: SecretStr`, `nora_tool_specs_dir: Path`. |
| `src/nora/hitl/tokens.py` | Modified | HMAC-SHA256 mint/verify; canonical payload; new `signature` field on `HitlApprovalToken`. |
| `src/nora/drivers/oid_catalog.py` | **Unchanged** (reference) | HMAC pattern mirrored; not modified. |
| `src/nora/server.py` | Modified | `snmp_run_spectrum_analysis` gains parameter; prompt registry boot wires multi-dir. |
| `src/nora/prompts/registry.py` | Modified | `scan(sources: Sequence[Path])`; `from_settings(settings)` reads `nora_tool_specs_dir`. |
| `src/nora/prompts/netops_orchestrator.md` | Modified | §6 added. |
| `src/nora/cli.py` + `__main__.py` | Modified | Sub-command dispatcher. `nora mcp` → MCP boot; `nora hitl mint …` → mint. No-args `nora` still boots MCP for back-compat. |
| `src/nora/drivers/exceptions.py` | Modified | New `Tier1ClearanceRequired(DriverError)`. |
| `src/nora/config.py` | Modified | Two new Settings fields. |
| `docs/tool_specs/*.md` (12 files) | New | Specs + README. |
| `tests/test_hitl_tokens.py`, `tests/test_prompts.py`, `tests/test_snmp_spectrum.py`, `tests/test_cli.py` | Modified | HMAC round-trip, multi-dir scan, gate raises, dispatcher back-compat. |

**Breaking changes:**
- `verify_approval_token`: tokens minted by the old stub (`stub-<op>-<ts>` with no signature) FAIL verification after this change. Existing tokens are invalidated at deploy time.
- `snmp_run_spectrum_analysis`: clients calling without `operator_confirmed` get a typed exception. Default `False` is fail-closed.
- CLI: any script invoking `nora hitl …` against the prior alias must move to the new dispatcher.

## Out of scope

- **Open WebUI changes.** The owner comment in #43 makes this explicit: NORA ships self-contained safety; Open WebUI is an external MCP client.
- **LLM-side enforcement layers outside NORA.**
- **Replacing the literal `AutonomousMutationRejected("autonomous device mutation rejected: HITL approval token required")` message contract** — pinned in `tests/test_hitl_tokens.py::migrate_autonomous_call_raises_autonomous_mutation_rejected`.
- **Phase-3 HITL ChangeRequest state machine** (token binding to ticket numbers, replay protection, audit-log emission, revocation list). This change closes the Phase-3 stub HMAC gap only.
- **Tool-spec schemas beyond 11 tools** listed below.

## Tier classification (verbatim from issue #43)

| Tier | Category | Operational Impact | Governance Policy |
|:-----|:---------|:-------------------|:------------------|
| **Tier 0** | Passive Telemetry (Read-Only) | Zero impact on subscriber traffic; non-disruptive query. | **Direct Execution** — orchestrator may invoke immediately. |
| **Tier 1** | Potentially Disruptive / Active Telemetry | CPU load, active RF sweeping, intrusive probing. | **Pause & Clearance Gate** — agent MUST explain necessity and request operator clearance. |
| **Tier 2** | Service-Affecting Mutations (Write / Config) | Interrupts RF link, drops SMs, alters carrier frequency, changes power, reboots. | **Strict HITL Gate** — halt. Valid ticket + maintenance window + cryptographic HITL token. |

**Tool mapping (all 11 tools):**

- **Tier 0 (Passive Reads):** `snmp_get_ap_summary`, `snmp_get_sm_table`, `snmp_get_pmp450i_radio_metrics`, `snmp_get_frame_utilization`, `snmp_get_sm_detailed_diagnostics`, `search_intervention_history`, `get_device_lifecycle_summary`, `correlate_sector_interference`.
- **Tier 1 (Clearance Required):** `snmp_run_spectrum_analysis`.
- **Tier 2 (HITL Token Required):** `snmp_migrate_radio_frequency`, `save_intervention_record`.

## HMAC approach

Mirror `OidCatalogRegistry._verify_one` (`src/nora/drivers/oid_catalog.py:477-540`). The verifier already raises `AutonomousMutationRejected` with a literal message; HMAC failure reuses the same exception so existing tests stay green and the wire-level contract is unchanged.

- **Algorithm:** `hmac.new(key_bytes, canonical_body, hashlib.sha256).hexdigest()`. Verification with **`hmac.compare_digest`** — non-negotiable; constant-time prevents timing oracles against the operator's signing key.
- **Canonical signing payload:** the tuple `(operator_id, issued_at_iso8601, expires_at_iso8601, token)` joined as `f"{operator_id}|{issued_at}|{expires_at}|{token}"` then UTF-8 encoded. A tuple — not the JSON dict — eliminates canonicalization bugs (key ordering, whitespace, separator drift). Spec phase freezes the exact string format.
- **`HitlApprovalToken` model gains `signature: str = Field(min_length=1)`** (frozen). Mint computes the canonical payload, signs with the key, returns the typed model.
- **Signing key:** from `Settings.nora_hitl_signing_key: SecretStr` — mirrors `Settings.nora_oid_catalog_signing_key` (catalog HMAC). Boot fails-closed (`CatalogVerificationError`-style typed error) if the key is empty or missing. Tests inject a hermetic `SecretStr` literal.
- **Wire format:** the JSON payload gains a `signature` field. Old stub tokens lack the field → `model_validate` rejects → typed `AutonomousMutationRejected` with the **literal** existing message. The literal wording is preserved verbatim so `test_migrate_autonomous_call_raises_autonomous_mutation_rejected` stays green.
- **Backward compatibility window:** **OPEN QUESTION (see §Open Questions).** The default plan is hard-break: tokens minted before the deploy are invalid. A dual-verify window (accept either stub OR HMAC) is rejected by default because it re-opens the forgeability the change exists to close; the proposal leaves it as a design-phase call.
- **Test strategy:** hermetic round-trip (`mint` → `verify`), tampered-payload rejection, wrong-key rejection, expired-token rejection, kill-switch (`NORA_HITL_TOKEN_TTL_SECONDS=0`) rejection, malformed-JSON rejection. All paths raise `AutonomousMutationRejected` with the literal message.

## Prompt composition approach — Approach A (chosen)

From the three options explore surfaced, **A — multi-dir `PromptRegistry.scan(...)`** is picked because it (i) keeps `PromptRegistry` the single source of truth for "what is registered at boot", (ii) reuses the existing front-matter validation (R5) with one additive field (`tier: 0 | 1 | 2`) — the contract for tool specs is a strict subset of the prompt contract, (iii) gives operators the familiar `Settings.nora_tool_specs_dir` override knob mirroring `nora_prompts_dir`, (iv) ships the smallest CLI delta (no new entry point needed), and (v) avoids the dual-registry drift risk Option C introduces.

**Mechanism (high level):**
- `PromptRegistry.scan(sources: Sequence[Path])` accepts one or more dirs (additive — single-dir call stays valid).
- `from_settings(settings)` reads `Settings.nora_tool_specs_dir` (default: `Path("docs/tool_specs")` resolved against the repo root, or `None` → skip tool-spec dir entirely if absent).
- Tool-spec files carry a `tier: 0 | 1 | 2` front-matter field in addition to `name` and `description`. The orchestrator prompt body references each tool's tier and behaviour by name; the LLM resolves the tool → spec lookup from the composed registry.

**Design phase decides:**
- Exact front-matter schema (`tier`, `requires_operator_confirmed`, `requires_hitl_token` — explore suggested these).
- Whether `tier` is enum-validated at scan time or free-form.
- How the orchestrator body references per-tool content (inline marker vs. `@mcp.prompt`-time name-lookup).

## Risks

| ID | Severity | Risk | Mitigation |
|----|----------|------|------------|
| R1 | CRITICAL → **resolved** | HMAC forgeability: `verify_approval_token` was a stub. The change closes it; HMAC-SHA256 with `hmac.compare_digest` is mandatory in the proposal. | This proposal. |
| R2 | CRITICAL (still open in design) | Tool-spec front-matter contract is undefined. Spec phase must freeze `tier` + `requires_operator_confirmed` + `requires_hitl_token` BEFORE `docs/tool_specs/*.md` files are authored, or all 12 files need to be re-touched. | Design phase produces the schema first; apply phase writes files against the frozen schema. |
| R3 | WARNING | `__main__` refactor ripples — `test_main_alias.py` + `test_integration_boot.py:323` pin `nora` (no-args) → MCP boot. | Spec phase pins a back-compat scenario: `nora` with no args → MCP boot; `nora mcp` → MCP boot; `nora hitl mint …` → mint; unknown sub-command → help + non-zero exit. |
| R4 | WARNING | FastMCP wire shape for `snmp_run_spectrum_analysis` changes. Existing MCP clients that omit `operator_confirmed` will see `False` (default) → typed exception. | Spec scenario covers both paths; orchestrator prompt instructs the LLM to negotiate clearance before invoking. |
| R5 | WARNING | Coverage threshold 85% (`openspec/config.yaml`). New code in `tokens.py`, `registry.py`, `exceptions.py`, `cli.py` increases the covered-module denominator. | Strict TDD applies (each new branch is test-first). Apply phase runs `uv run python -m pytest --cov=src/nora` and refuses merge below threshold. |
| R6 | WARNING | Existing tokens invalidated at deploy. Any operator session mid-flight will see `AutonomousMutationRejected`. | Document in release notes; the kill switch (`NORA_HITL_TOKEN_TTL_SECONDS=0`) is a documented backout. |
| R7 | SUGGESTION | Catalog envelope could gain a `tier` field so `OidCatalogRegistry` boot-time guard refuses to expose Tier-2 tools without an HMAC key. Out of scope per issue body; flag for the Phase-3 cluster. | Follow-up issue after archive. |

## Open questions for design phase

1. **Prompt composition mechanism (Approach A details).** Exactly how does the orchestrator body reference per-tool content? Inline-marker (`<!-- tool: snmp_run_spectrum_analysis -->`) resolved at scan time, or runtime name-lookup at `@mcp.prompt` call time? Pick one and document the trade-off.
2. **Backward-compat window for legacy stub tokens.** Hard-break (default) vs. dual-verify window (e.g. accept either format for one release) vs. versioned `version: 2` field on the token model. Default is hard-break; design must explicitly reject the dual-verify option with rationale.
3. **CLI dispatcher back-compat test pinning.** Should the new `nora hitl mint` command also be exposed as `nora-hitl` (a separate console script) for environments that can't shell-escape the space? Default: no; `nora hitl mint` is the only entry point.
4. **Tool-spec front-matter schema.** Beyond `tier`, do we need `requires_operator_confirmed: bool` and `requires_hitl_token: bool`? Or is `tier` enough since it implies the prerequisites? Design must freeze the field set.
5. **Signing-key rotation story.** How does an operator rotate `nora_hitl_signing_key` without invalidating in-flight tokens? Design-phase decision; HMAC gap closure does not require a rotation mechanism but the issue body hints at one.
6. **Coverage threshold impact.** Apply phase should report the new covered-module denominator; if a single PR threatens to drop below 85%, the design phase should pre-commit to splitting work.

## Capabilities contract (handoff to sdd-spec)

### New Capabilities

- `tool-service-impact-tiers`: The 3-tier classification (Tier 0 / 1 / 2), the governance policy per tier, the tool-to-tier mapping, and the cross-reference to `docs/tool_specs/*.md`. Lands at `openspec/changes/2026-09-15-3tier-tool-governance/specs/tool-service-impact-tiers/spec.md`.

### Modified Capabilities

- `pmp450i-radio-tools`: `snmp_run_spectrum_analysis` gains `operator_confirmed: bool = False`; the server-side gate raises a typed exception on False (or absent) BEFORE any SNMP GET. Migration slice unchanged.
- `prompt-registry`: additive R8 — multi-dir scan via `Settings.nora_tool_specs_dir`; tool-spec front-matter contract (`tier`, etc.) defined here. `from_settings` reads the new setting; single-dir `scan(Path)` stays valid.
- `nora-mcp-server`: new `nora hitl mint` sub-command; FastMCP wire shape for spectrum tool gains `operator_confirmed`; boot wires the multi-dir prompt registry.
- `secure-configuration`: two new `Settings` fields — `nora_hitl_signing_key: SecretStr`, `nora_tool_specs_dir: Path | None`. Boot fails closed if the signing key is missing when a Tier-2 tool is invoked.

## Rollback plan

1. **HMAC rollback.** Revert `tokens.py` to the JSON-only stub. All in-flight tokens re-validate (they were either HMAC-signed by the new `mint` and pass, or stub tokens that already failed — no recovery from a forge). Document in release notes; treat as a deliberate regression to a known-insecure state.
2. **Tier-1 gate rollback.** Revert `snmp_run_spectrum_analysis` signature; the prior behaviour (autonomous spectrum sweep on LLM call) returns.
3. **CLI dispatcher rollback.** Restore `__main__.py` deprecation-alias; remove `nora hitl` sub-command. Existing alias tests go green.
4. **Tool-spec rollback.** Delete `docs/tool_specs/` and the `Settings.nora_tool_specs_dir` wiring. Orchestrator prompt reverts to its pre-change body (drop §6).
5. **Order of operations.** Roll back HMAC FIRST (closes the forgeability window the change exists to close — do not leave it open). Then CLI, then tool-spec, then Tier-1 gate. Each step is independently revertible via `git revert` of the corresponding commit.

## Dependencies

- **None external.** All primitives already exist: `hmac`, `hashlib`, `pydantic`, `SecretStr`, the `OidCatalogRegistry` HMAC pattern. No new dependency.
- **Internal.** `src/nora/drivers/exceptions.py` (new `Tier1ClearanceRequired`); `src/nora/config.py` (new `Settings` fields). Both live in the same package; no cross-package coupling.

## Success criteria

- [ ] All 11 NORA MCP tools are classified Tier 0 / 1 / 2 per the verbatim table above.
- [ ] `docs/tool_specs/` contains 12 files (11 specs + README), each authored against the frozen front-matter schema.
- [ ] `netops_orchestrator.md` contains a §6 "Universal Service Impact & Disruption Gate" that references every tier and the operator-clearance protocol.
- [ ] `snmp_run_spectrum_analysis(device_id, operator_confirmed=False)` raises `Tier1ClearanceRequired` BEFORE any SNMP GET; `operator_confirmed=True` proceeds inside the maintenance window.
- [ ] `nora hitl mint --operator-id <id> --ttl-seconds 900` prints a JSON token carrying `signature`; pasting it into the migration tool verifies without raising.
- [ ] `verify_approval_token` uses `hmac.compare_digest`; legacy stub tokens raise `AutonomousMutationRejected` with the literal message.
- [ ] `PromptRegistry` scans both `src/nora/prompts/` and `Settings.nora_tool_specs_dir`; the orchestrator prompt body references each tool's tier.
- [ ] `nora` with no args boots MCP (deprecation-alias tests stay green); `nora mcp` boots MCP; `nora hitl mint` dispatches; unknown sub-command exits non-zero with help.
- [ ] `uv run python -m pytest --cov=src/nora` passes at ≥85% coverage.
- [ ] `uv run ruff check . && uv run ruff format --check . && uv run mypy --strict src/nora` is clean.

---

**Next phase:** `sdd-spec` consumes this proposal + the explore artifact and writes `openspec/changes/2026-09-15-3tier-tool-governance/specs/{tool-service-impact-tiers,...}/spec.md` with Given/When/Then scenarios per the four modified + one new capability above. Open questions in §Open Questions feed `sdd-design` decisions BEFORE apply.
