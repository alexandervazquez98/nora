# Feature: Issue #45 — Prompt Versioning, SSoT Governance, Open WebUI Sync

**Status**: 🚧 Planned — design frozen; implementation not started.
**Branch**: `feat/issue-45-prompt-versioning` (from `origin/main` @ `4c6399a` — v0.3.5 release commit).
**Issue**: [#45](https://github.com/alexandervazquez98/nora/issues/45) — `rfc(prompting/ops): system prompt versioning, SSoT governance, and Open WebUI automated sync`.
**Depends on**: v0.3.4 (issue #72) prompt-spec-exposure — `nora_get_tool_spec` bridge + per-tool `@mcp.prompt` are the foundation.

## Why this doc exists

Today, system prompts (`src/nora/prompts/*.md`) carry only `name` + `description` front-matter and ship silently with every release. When a prompt is updated, Open WebUI operators have no machine-readable signal that the deployed LLM context has changed; drift between NORA's canonical prompt and the chat front-end's Modelfile is undetectable, and rollback requires manual surgery. Issue #45 proposes three levers to close this gap:

1. **SSoT in Git with rich front-matter** — each prompt carries `version`, `nora_compatibility`, `governance`, and `checksum_sha256` so its identity is queryable.
2. **Open WebUI declarative sync** — a CLI subcommand pushes versioned model profiles (`nora-netops:v0.3.5`, `nora-netops:latest`) into the chat front-end via REST.
3. **Instant rollback** — versioned profiles are immutable; selecting a prior tag in Open WebUI reverses a misbehaving deployment without SSH access.

## Design Decisions (frozen 2026-09-20)

- **Q1 — `checksum_sha256` provenance (Recommended: scan-time compute + validate-match).** `PromptRegistry._load_prompt_file` computes `sha256(body_bytes)` at scan time. If the front-matter declares a `checksum_sha256` that does not match, raise `PromptNotFoundError`. The operator cannot lie: drift is detected at boot. *(Decision: 2026-09-20, user-approved.)*
- **Q2 — `nora_compatibility` semantics (Recommended: SemVer range vs `nora.__version__`).** Front-matter declares a range like `>=0.3.4,<0.4.0`. Boot validates against `nora.__version__`; non-satisfying range raises `PromptNotFoundError` and the prompt is dropped. Fails closed. *(Decision: 2026-09-20, user-approved.)*
- **Q3 — Sync trigger (Recommended: manual-only).** `nora prompt sync` is invoked explicitly by the operator. No automatic sync on boot. Safe, auditable, predictable. *(Decision: 2026-09-20, user-approved.)*

### Frozen in spec (already from issue #72)

- **Q1 (composition) — per-tool `@mcp.prompt`.** Tool specs are MCP prompts with the same name as the tool.
- **Q4 (front-matter schema) — tier-conditional mandatory.** `name`, `description`, `tier: 0|1|2` required; `requires_operator_confirmed` when tier=1; `requires_hitl_token` when tier=2.

### Frozen by this feature

- **Q5 — SemVer semantics for prompt `version`.** Patch = clarity/typo fixes; Minor = tool additions or new governance rules; Major = structural / behavioral breaking. Mirrors NORA's own SemVer.
- **Q6 — Watermark banner format.** `<!-- NORA-PROMPT: <name> v<version> [sha: <first-8-hex>] -->\n\n<body>`. SHA truncated to 8 hex chars to avoid HMAC-fragment leak concerns (per the PR-zero-leak playbook).
- **Q7 — Versioned model profile naming.** `<model>-v<X.Y.Z>` is immutable (POST creates a new frozen profile). `<model>-latest` is the mutable alias (PUT updates it in place). Both carry metadata `{commit_sha, release_tag, synced_at, nora_version, prompt_version}`.
- **Q8 — Tool-specs vs system-prompts front-matter split.** Tool-specs (`docs/tool_specs/*.md`) keep the **tier-based** schema (name + description + tier + tier-conditional required flags). System-prompts (`src/nora/prompts/*.md`) use the **version-based** schema (name + description + version + nora_compatibility + governance + checksum_sha256). Two distinct schemas, one registry, one validator per class.

## Architectural Anchors

- `src/nora/prompts/registry.py` — `PromptRegistry.scan`, `from_settings`, `_validate_tool_spec`. Will gain `_validate_system_prompt` and a `render(name)` method.
- `src/nora/prompts/netops_orchestrator.md`, `src/nora/prompts/snmp_pmp450i.md` — system prompts that gain the version-based front-matter.
- `src/nora/server.py` — FastMCP server. Wrappers will switch from `PromptRegistry.get` to `PromptRegistry.render` so the watermark reaches the LLM.
- `src/nora/__main__.py` — argv dispatcher. Gains `nora prompt sync` subcommand.
- `scripts/sync_openwebui_model.py` (NEW) — or implemented as a sub-module of `src/nora/prompts/sync.py` with a thin script wrapper.
- `OPERATIONS.md` (root) — extended with PromptOps workflow + rollback procedure.
- `openspec/specs/prompt-registry/spec.md` — gains 4 new requirements (Versioning, Checksum, Compatibility Range, Watermark, Sync) with scenarios.

## Out of Scope (this feature)

- **Modelfile content rewriting** — we ship the prompt body to Open WebUI as the `system` field. We do not generate or modify Open WebUI Modelfile YAML templates.
- **Chat-template scaffolding** — Open WebUI's chat template / function-calling rules are out of scope; the sync only registers the system prompt + tool list.
- **Bidirectional sync** — NORA only writes to Open WebUI. Reading prompt edits back from Open WebUI into Git is explicitly rejected (breaks the SSoT guarantee).
- **Multi-tenant Open WebUI** — sync targets ONE base URL at a time. Multi-tenant orchestration is a future concern.
- **Auto-rollback** — rollback is operator-initiated by selecting a frozen tag in Open WebUI. No automation around it.

## Work Units

Each WU = one work-unit commit on `feat/issue-45-prompt-versioning`. Conventional Commit messages. Tests alongside behavior. `--no-verify` because the `.gga` hook requires a configured LLM provider.

### WU-1 — Frontmatter schema extension + registry validator

**Touch**:
- `src/nora/prompts/registry.py`:
  - New `_validate_system_prompt(name, front_matter, body_bytes)` method. Schema: `name`, `description`, `version`, `nora_compatibility`, `governance`, `checksum_sha256`.
  - SemVer validation for `version` (regex `\d+\.\d+\.\d+`).
  - SemVer range validation for `nora_compatibility` (use `packaging.specifiers.SpecifierSet`) against `nora.__version__`.
  - `governance` must be a dict with int values for `tier_0`, `tier_1`, `tier_2` (all non-negative).
  - `checksum_sha256` must be 64-char hex; compute `hashlib.sha256(body_bytes).hexdigest()` and assert equality; raise `PromptNotFoundError` on mismatch.
  - The existing `_validate_tool_spec` (tier-based) stays untouched for `docs/tool_specs/*.md`.
- `src/nora/prompts/netops_orchestrator.md` — add `version`, `nora_compatibility`, `governance`, `checksum_sha256` to the existing front-matter.
- `src/nora/prompts/snmp_pmp450i.md` — same.
- `tests/test_prompts.py` — add tests:
  - `test_registry_rejects_prompt_with_mismatched_checksum`
  - `test_registry_rejects_prompt_with_invalid_nora_compatibility`
  - `test_registry_rejects_prompt_with_non_semver_version`
  - `test_registry_accepts_prompt_with_valid_frontmatter`
  - `test_registry_drops_prompt_when_nora_compatibility_unsatisfied`
  - `test_tool_specs_keep_tier_based_schema` (regression — confirm docs/tool_specs/*.md validation path unchanged)

**Behavior change**: `PromptRegistry.scan()` now fail-closes if a system prompt is missing `version` or `checksum_sha256` mismatches. Boot-time impact only.

**Evidence**: full pytest suite, with new tests targeted at the 6 new cases.

### WU-2 — Watermark banner + render() method

**Touch**:
- `src/nora/prompts/registry.py`:
  - New `render(name: str) -> str` method on `PromptRegistry`. Returns:
    `<!-- NORA-PROMPT: <name> v<version> [sha: <first-8-hex-of-checksum>] -->\n\n<original-body>`
  - Keep `get(name) -> str` returning the raw body for backward compat.
- `src/nora/server.py`:
  - All `@mcp.prompt` wrappers (the 2 system prompts + 15 tool specs via nora_get_tool_spec) switch from `registry.get(name)` to `registry.render(name)`.
- `tests/test_prompts.py` — add tests:
  - `test_render_includes_watermark_with_correct_format`
  - `test_render_watermark_sha_is_first_8_hex_chars`
  - `test_render_unknown_prompt_raises_promptnotfounderror`

**Behavior change**: the LLM now sees a watermark banner at the top of every prompt body. No protocol-level change.

**Evidence**: existing test_prompts.py (41 tests) still pass; 3 new tests pass.

### WU-3 — Open WebUI sync script

**Touch**:
- `src/nora/prompts/sync.py` (NEW):
  - `OpenWebUIConfig(base_url: str, admin_api_key: str)` — pydantic-settings model.
  - `OpenWebUIClient(config).upsert_model(model_id, system_prompt, metadata)` — POST/PUT to `/api/v1/models`.
  - `sync_prompts(registry, config, nora_version, git_sha)` — orchestrates: render each registered prompt, build versioned profile names, POST immutable v-tagged profile, PUT latest alias.
  - Pure stdlib + httpx (matches existing `spectrum_http.py` pattern).
- `scripts/sync_openwebui_model.py` (NEW) — thin CLI wrapper: load `.env`, instantiate `OpenWebUIConfig`, instantiate `OpenWebUIClient`, call `sync_prompts`, log diff.
- `src/nora/__main__.py` — add `nora prompt sync` subcommand that calls the script programmatically (avoids subprocess overhead).
- `tests/test_prompts_sync.py` (NEW):
  - Mock httpx client; assert POST for `nora-netops:v<X.Y.Z>` once; PUT for `nora-netops:latest` once; metadata dict carries commit_sha + synced_at + nora_version.
  - Test idempotency: re-running with the same version produces a no-op (or PUT-with-same-body).
  - Test error path: 401 from Open WebUI → clear error message + non-zero exit.
- `tests/test_cli.py` — add a smoke test for `nora prompt sync` argv path.

**Behavior change**: a new CLI subcommand. No existing flow changes.

**Evidence**: new test file green; existing CLI tests unaffected.

### WU-4 — OPERATIONS.md + spec doc extensions

**Touch**:
- `OPERATIONS.md` (root) — add `## PromptOps Workflow` section + `## Instant Rollback` section:
  - When to bump patch vs minor vs major.
  - How to run `nora prompt sync` (env vars, expected output).
  - Rollback procedure (select frozen tag in Open WebUI dropdown).
  - Troubleshooting: checksum mismatch → how to recover.
- `INSTALL.md` — add a section "Prompt synchronization" listing the env vars and the post-install `nora prompt sync` step.
- `openspec/specs/prompt-registry/spec.md` — add 4 new requirements:
  - `System-Prompt Versioning` — version + SemVer + governance metadata.
  - `Checksum Drift Detection` — checksum_sha256 computed at scan + matched against declaration.
  - `Compatibility Range Enforcement` — nora_compatibility range vs `nora.__version__`.
  - `Watermark Banner Rendering` — render() output format.
  - `Open WebUI Sync` — versioned profile creation + latest alias update.
  - Each requirement with 2–3 scenarios. Spec scenarios: 29 → ~40.

**Behavior change**: documentation-only. No code path changes.

**Evidence**: spec link + OPERATIONS.md render preview.

## Sequencing

WU-1 → WU-2 → WU-3 → WU-4. Each unblocks the next:
- WU-2 depends on WU-1 (the `version` + `checksum_sha256` fields must exist before `render()` can use them).
- WU-3 depends on WU-2 (sync must call `render()` to get the watermark banner into Open WebUI).
- WU-4 depends on WU-1/WU-2/WU-3 (docs reference all three).

## Branch & Commits

- Branch: `feat/issue-45-prompt-versioning` (off `main` @ `4c6399a`).
- WU-1 → WU-4 commits, in order, each `--no-verify`.
- After all 4 land: `git push -u origin feat/issue-45-prompt-versioning`, then `gh pr create`.
- Merge + release follows the standard playbook (squash + delete-branch + bump + tag + GitHub release).

## Definition of Done

- `pytest tests/` green (full suite).
- `ruff check src/nora tests` clean.
- `mypy src/nora` clean (44 source files, strict).
- All 4 WU commits land on the feature branch.
- The PR body contains the zero-leak pass (no HMAC fragments, no operator naming patterns, no production-environment references).
- The PR is reviewed and merged via squash.
- Version bumped (e.g. `0.3.5 → 0.4.0` if any WU introduced a Minor signal; otherwise `0.3.5 → 0.3.6`).
- GitHub release with changelog published.
- Issue #45 closed via `Closes #45` keyword.

## Related

- Issue: https://github.com/alexandervazquez98/nora/issues/45
- Spec: `openspec/specs/prompt-registry/spec.md` (will gain 5 new requirements)
- Predecessor feature: `feat/prompt-spec-exposure` (v0.3.4, PR #73) — `nora_get_tool_spec` bridge.
- Successor candidates: issue #60 (TUI installer that calls `nora prompt sync` on update).
