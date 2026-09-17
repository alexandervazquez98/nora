# Exploration: 3-Tier Tool Governance + Modular Tool Specs (issue #43, Scope B+)

> **Scope reminder.** User-confirmed scope: **B+** — RFC + dynamic
> composition + server-side enforcement, **entirely inside NORA Core**.
> Open WebUI is treated strictly as an external MCP client. No
> Open WebUI changes. No LLM-side enforcement layers in Open WebUI.
> See `openspec/changes/2026-09-15-3tier-tool-governance/issue-43-body.md`
> (captured from `gh issue view 43` on 2026-09-15) for the verbatim
> issue body + the owner's confirming comment.
>
> **Proposed change name:** `2026-09-15-3tier-tool-governance` —
> matches the repo's `YYYY-MM-DD-<slug>` convention (cf.
> `2026-09-13-pmp450i-production-surface`,
> `2026-09-15-register-device-mcp`).
> **Confirmed; no rename.**

---

## Current State

### Tool surface (12 named items — all 11 tools exist, plus the README)

| Tool | Tier | Defined at | Notes |
|------|------|------------|-------|
| `snmp_get_ap_summary` | 0 | `server.py:192-211` → `summaries.fetch_ap_summary` | Exists. |
| `snmp_get_sm_table` | 0 | `server.py:242-266` → `subscribers.fetch_sm_table` | Exists. Cross-checks `PRE_DIAGNOSTIC` history before categorise. |
| `snmp_get_pmp450i_radio_metrics` | 0 | `server.py:161-174` → `driver.fetch_radio_metrics` | Exists (legacy, the first `@mcp.tool`). |
| `snmp_get_frame_utilization` | 0 | `server.py:213-225` → `summaries.fetch_frame_utilization` | Exists. |
| `snmp_get_sm_detailed_diagnostics` | 0 | `server.py:268-285` → `subscribers.fetch_sm_detailed_diagnostics` | Exists. `luid` is the per-LUID key. |
| `search_intervention_history` | 0 | `server.py:378-414` → `intervention_tools.search_intervention_history` | Exists. Read-only. |
| `get_device_lifecycle_summary` | 0 | `server.py:417-437` → `intervention_tools.get_device_lifecycle_summary` | Exists. Read-only. |
| `correlate_sector_interference` | 0 | `server.py:440-468` → `intervention_tools.correlate_sector_interference` | Exists. Read-only. |
| `snmp_run_spectrum_analysis` | **1** | `server.py:300-321` → `spectrum.fetch_spectrum` | Exists. Current signature: `(device_id: str) -> dict[str, Any]`. **No** `operator_confirmed` parameter today. |
| `snmp_migrate_radio_frequency` | **2** | `server.py:337-370` → `migrate.fetch_migrate` | Exists. Already calls `verify_approval_token` first (Tier-2 gate is in place). |
| `save_intervention_record` | **2** | `server.py:476-497` → `intervention_writer.writer.save_intervention_record` | Exists. Records-keeping blast radius (deletable file) — no HITL gate today. |
| `docs/tool_specs/README.md` | n/a | **does not exist** | Must be authored. |

Total: **11 tool entries in the registry + 1 new `docs/tool_specs/README.md` = 12 files** under `docs/tool_specs/`. The user's "12 files" count matches the issue's own list.

### `PromptRegistry` composition (`src/nora/prompts/registry.py`)

* Single source dir: either `src/nora/prompts/*.md` (package default) **or** `Settings.nora_prompts_dir` (operator override).
* `scan(source: Path)` globs `*.md`, parses YAML front-matter, validates `name == filename.stem` + non-empty `description`. Silently drops invalid files; surfaces as `PromptNotFoundError` on `get(name)`.
* Frozen for the lifetime of the process — **no hot-reload, no inotify** (R1).
* `from_settings(settings)` is the only boot entry point.

**Implication for this change.** `docs/tool_specs/*.md` is a **second directory** the registry has never loaded. There is no multi-dir scan today. The composition mechanism for issue #43 is an **open design question** — see `Approaches` below. The proposal/design phases own the decision; explore only documents the shape.

### Tier-1 enforcement surface

* `snmp_run_spectrum_analysis(device_id: str) -> dict[str, Any]` — `server.py:300-321`. Thin delegate to `drivers.snmp_pmp450i.spectrum.fetch_spectrum(...)`.
* `fetch_spectrum` already raises `MaintenanceWindowViolation` (`drivers/exceptions.py:131-142`) when outside `Settings.nora_maintenance_window_*`. Existing typed-error pattern: **subclass `DriverError` and let FastMCP surface the literal message**.
* **New parameter** to add: `operator_confirmed: bool = False`. On `False` (or absent) the tool MUST raise a typed exception (e.g. `Tier1ClearanceRequired`) **before any wire frame is emitted** (mirror the spectrum-window invariant: gate fires first, no SNMP GET).
* Open question for design: where does the gate live — `server.py` wrapper, `fetch_spectrum` body, or a new helper? See Approaches.

### Tier-2 token infrastructure (what the issue's "HMAC validation already exists" claim **does** and **does not** mean)

* `src/nora/hitl/tokens.py` ships:
  * `HitlApprovalToken` (frozen Pydantic model: `token`, `operator_id`, `issued_at`, `expires_at`).
  * `mint_token(operator_id, *, ttl_seconds=900)` — emits a stub token string keyed on `operator_id` + `int(now.timestamp())`.
  * `verify_approval_token(token: str | None) -> HitlApprovalToken` — parses JSON, validates against the Pydantic schema, checks `expires_at > now`, checks `hitl_kill_switch_active()`.
* `verify_approval_token` **does NOT validate an HMAC signature**. The token payload is `{token, operator_id, issued_at, expires_at}` and there is no `signature` / `hmac_sha256` field. **Any operator can forge a token that satisfies the current verifier** — this is a stub, not a security gate.
* `Settings.nora_hitl_token_ttl_seconds` (default 900) and `hitl_kill_switch_active()` (`NORA_HITL_TOKEN_TTL_SECONDS=0`) are the only server-side knobs.
* `snmp_migrate_radio_frequency` calls `verify_approval_token(approval_token)` as the very first line of its body (`server.py:363` → `migrate.fetch_migrate` → `migrate.py:245`).

> **CRITICAL GAP (must surface in proposal).** The user wrote
> "HMAC validation already exists in `verify_approval_token` — reuse it."
> The verifier exists; the **HMAC does not**. The proposal MUST
> decide: (a) add HMAC-SHA256 signing to the existing token model
> (mirroring `OidCatalogRegistry` HMAC pattern in
> `drivers/oid_catalog.py:33,535` + `scripts/sign_catalog.py:179`), or
> (b) ship the `mint` CLI on top of the current JSON-only stub and
> document the forgeability as a known limitation. The repo already
> has all the primitives needed for (a): `hmac`, `hashlib`, the
> canonical-body pattern, `SecretStr` settings, and the
> `scripts/sign_catalog.py` precedent.

### CLI surface — `nora hitl mint` does not exist yet

* `pyproject.toml` declares exactly two scripts: `nora = "nora.__main__:main"` and `nora-mcp = "nora.cli:main"`.
* `src/nora/__main__.py` is a **deprecation alias** that emits `DeprecationWarning` and delegates to `nora.cli.main`. It has no sub-command parsing.
* `src/nora/cli.py` uses `argparse` (NOT Typer). The parser accepts `--transport`, `--host`, `--port`, `--path`, `--stateless-http` and immediately boots the FastMCP server — no sub-command dispatcher.
* `scripts/sign_catalog.py` is a standalone script (separate console-script entry) and is **not** the model the issue intends — `nora hitl mint` is expected to ship as an operator CLI, not a build-time helper.

> **Open design question.** How does `nora hitl mint ...` get wired?
> Three options — see Approaches. None exists today.

### PromptRegistry — composition design question

The orchestrator prompt at `src/nora/prompts/netops_orchestrator.md` is 66 lines, with §1–§5 (Zero-Leakage, Language, Unbiased Baseline, Intervention-Memory Sequence, HITL & Safe Migration Gate). The issue adds a **Universal Service Impact & Disruption Gate** (presumably a new §6) AND expects each tool's behaviour to be **composed from `docs/tool_specs/<tool_name>.md`** rather than re-stated in the orchestrator prompt.

Composition mechanism — three plausible options, all open for design:

1. **Multi-dir `PromptRegistry.scan(...)`** — extend `scan` (or add `from_settings(scan_dirs=[...])`) so it walks the packaged prompts AND `docs/tool_specs/*.md` at boot. The orchestrator prompt's `body` references tool specs by name (LLM-side composition). Pros: keeps the registry the single source of truth; reuses front-matter validation; `Settings.nora_tool_specs_dir` becomes a new override knob mirroring `prompts_dir`. Cons: changes R1–R7 contract — design phase owns the additive R8.
2. **Inline-merge at boot in `cli.main()`** — read `docs/tool_specs/*.md` separately, concatenate their bodies into the orchestrator prompt body **before** injecting the registry. Pros: zero changes to `PromptRegistry`. Cons: bypasses front-matter validation; hides the composition step from the registry contract; harder to override.
3. **Separate `ToolSpecRegistry`** — registry for tool specs, distinct from prompts. The orchestrator prompt is augmented by name-lookup at `@mcp.prompt netops_orchestrator` call time. Pros: clean separation of concerns. Cons: new module + new contract; arguably over-engineered for 12 files.

The design phase picks one. Explore only logs the surface.

### Existing tests (where new tests land)

* `tests/test_prompts.py` — 12 scenarios R1–R12 covering `PromptRegistry` plus orchestrator content scan + `@mcp.prompt` enumeration + `set_prompt_registry` lifecycle. New tier-enforcement tests likely land here (or in a sibling `tests/test_prompts_3tier_governance.py`).
* `tests/test_prompts_register_device_clause.py` — three section-4 content scans (regex-based). Pattern is reusable for the new Service Impact Gate section.
* `tests/test_hitl_tokens.py` — six named scenarios for the stub verifier (literal message, round-trip, kill-switch, expired, malformed JSON). The new `mint` CLI tests likely append here or to a sibling `tests/test_hitl_cli.py`.
* `tests/test_snmp_spectrum.py` — six scenarios covering ranked candidates, window enforcement, defensive coverage. New `operator_confirmed` gate tests likely append here (with content-based scans for the orchestrator prompt mentioning the gate in `tests/test_prompts.py`).
* `tests/test_cli.py` — six scenarios covering boot sequence, env vars, flags, help, exit codes. New sub-command tests likely append here.

---

## Affected Areas

* `openspec/specs/pmp450i-radio-tools/spec.md` — slice 3 ("spectrum refuses outside the window") needs to gain the operator-confirmed prerequisite.
* `openspec/specs/prompt-registry/spec.md` — if the design picks Options 1 or 3, the contract gains an additive R8 (multi-dir scan OR tool-spec registry).
* `src/nora/server.py` — `@mcp.tool snmp_run_spectrum_analysis` gains `operator_confirmed: bool = False`; `@mcp.tool snmp_migrate_radio_frequency` is unchanged (gate already in place); `@mcp.prompt netops_orchestrator` body may gain a composition seam.
* `src/nora/prompts/registry.py` — possibly extended (Option 1) or unchanged (Options 2/3).
* `src/nora/prompts/netops_orchestrator.md` — gains the **Universal Service Impact & Disruption Gate** section; the 3-tier taxonomy must appear in the body.
* `src/nora/drivers/exceptions.py` — new typed exception class (e.g. `Tier1ClearanceRequired`) inheriting `DriverError`. Existing pattern at `:131-142` (MaintenanceWindowViolation).
* `src/nora/drivers/snmp_pmp450i/spectrum.py` — `fetch_spectrum` gains the `operator_confirmed` check (or `server.py` wrapper enforces it). Design decision.
* `src/nora/hitl/tokens.py` — possibly extended with HMAC-SHA256 signature + `Settings.nora_hitl_signing_key` (if HMAC gap is closed). Pydantic model gains a `signature` field.
* `src/nora/config.py` — new settings: `nora_hitl_signing_key: SecretStr | None`, `nora_tool_specs_dir: Path | None`. Mirror the existing `nora_prompts_dir` + `nora_oid_catalog_signing_key` patterns.
* `src/nora/cli.py` (or new `src/nora/cli_hitl.py`) — sub-command dispatch for `nora hitl mint`.
* `src/nora/__main__.py` — refactor to dispatch on `argv[0]` (sub-command), or leave as alias and add a new console script.
* `pyproject.toml` — possible new `[project.scripts]` entry (`nora-hitl`?) if Option B is picked for the CLI wiring.
* `docs/tool_specs/README.md` + 11 `docs/tool_specs/<tool_name>.md` — new documentation; English, scoped to one tool each.
* `tests/test_prompts.py` — append content-scan tests for the Service Impact Gate section AND each tool name appearing in the orchestrator body.
* `tests/test_prompts_register_device_clause.py` — sibling `tests/test_prompts_service_impact_gate.py` mirroring its regex-section pattern.
* `tests/test_hitl_tokens.py` — append HMAC mint+verify round-trip tests (if gap is closed) and `mint` CLI tests.
* `tests/test_snmp_spectrum.py` — append `operator_confirmed=False` raises typed exception + zero wire frames; `operator_confirmed=True` proceeds inside the window.
* `tests/test_cli.py` — append `nora hitl mint` happy path + error path (kill switch + bad operator id + bad TTL).

---

## Approaches

### Approach A — Composition: multi-dir `PromptRegistry.scan(...)`; CLI: refactor `nora` into a dispatcher; HITL: HMAC-SHA256

* **Composition.** Extend `PromptRegistry` so its `scan(...)` accepts either a single dir (today) or a list/tuple of dirs (new). The boot path in `cli.main()` calls `scan([_PACKAGED_PROMPTS_DIR, settings.nora_tool_specs_dir])`. The orchestrator prompt references each tool spec by name; the LLM resolves the tool → spec mapping from the composed prompt.
* **CLI.** Refactor `nora.__main__:main` so that `argv` is parsed first for a sub-command: `nora mcp` → `cli.main()`; `nora hitl mint` → new `cli_hitl.mint(...)`. The existing `nora-mcp` entry point stays untouched for back-compat.
* **HITL.** Add `Settings.nora_hitl_signing_key: SecretStr | None`. Extend `HitlApprovalToken` with `signature: str`. `mint_token(...)` produces a canonical JSON body and signs with `hmac.new(key, body, sha256)`. `verify_approval_token(...)` recomputes and `hmac.compare_digest`s. Mirror the `OidCatalogRegistry` HMAC pattern (`drivers/oid_catalog.py:33,535`).

| Pros | Cons |
|------|------|
| Keeps `PromptRegistry` the single source of truth; minimal new module surface. | Touches `PromptRegistry.scan(...)` contract → R1–R7 spec gets an additive R8 (small but real change). |
| LLM has one composed prompt — no name-lookup runtime logic. | Larger prompt body (11 tool specs appended). |
| Dispatcher CLI matches Unix conventions; reusable for future `nora hitl revoke`, `nora hitl list`, etc. | Backwards-compat risk on `python -m nora` — needs explicit deprecation handling for sub-commands. |
| HMAC closes the forgeability gap. | New secret in `Settings` (`.env` write); boot fails-closed on missing key (mirrors catalog key). |
| Mirrors the catalog HMAC pattern → operators already understand it. | `nora_hitl_signing_key` rotation requires re-issuing tokens; needs a story. |

**Effort:** Medium-High. 4 modules + 1 spec + tests.

### Approach B — Composition: inline-merge in `cli.main()`; CLI: new entry point `nora-hitl`; HITL: keep stub

* **Composition.** Leave `PromptRegistry` alone. In `cli.main()`, after `prompt_registry.get("netops_orchestrator")`, append the body of each `docs/tool_specs/*.md` to the returned body before the registry freezes. The `PromptRegistry.get(...)` returns the merged body.
* **CLI.** Add `[project.scripts] nora-hitl = "nora.cli_hitl:main"` to `pyproject.toml`. The `nora` command stays as-is (still MCP-boot). The `nora hitl mint ...` invocation in the issue becomes `nora-hitl mint ...`.
* **HITL.** Reuse the existing JSON-only verifier as-is. Document the forgeability in the new `docs/tool_specs/README.md` as a known limitation that the Phase-3 ChangeRequest cluster will close.

| Pros | Cons |
|------|------|
| Zero changes to `PromptRegistry`. | Bypasses front-matter validation for tool specs (no `name` / `description` enforcement). |
| Pure content in `docs/`, pure code in `src/nora/`. | Hidden composition step — reader can't grep `PromptRegistry` to find the merge. |
| New entry point is the smallest CLI delta. | `nora hitl mint` (as the issue reads) won't work — operator types `nora-hitl mint`. **Friction.** |
| No new secret; no rotation story. | Tokens remain forgeable. **The whole premise of the change is to remove operator-vs-LLM ambiguity; an LLM can still mint tokens.** |
| Lowest-effort option overall. | Closest to a stub of the RFC. |

**Effort:** Low. 2 modules + tests + docs.

### Approach C — Composition: separate `ToolSpecRegistry`; CLI: dispatcher; HITL: HMAC

* **Composition.** New `src/nora/tool_specs/registry.py` modelled on `PromptRegistry`. `ToolSpecRegistry.scan(settings.nora_tool_specs_dir)` loads every `docs/tool_specs/*.md`, validates front-matter (lighter — just `tool_name` + `tier`), exposes `get(tool_name) -> ToolSpec`. The orchestrator prompt is augmented at `@mcp.prompt netops_orchestrator` call time by joining the registered tool specs.
* **CLI.** Refactor `nora` to dispatcher (same as Approach A's CLI choice).
* **HITL.** Same HMAC closure as Approach A.

| Pros | Cons |
|------|------|
| Cleanest separation: prompts vs tool specs are different artefacts. | New module + new contract. |
| Future-friendly (tool specs may grow to per-firmware, per-vendor). | Two registries to keep in sync — operators must remember to update both. |
| HMAC closes the forgeability gap. | Larger change footprint. |

**Effort:** High. 5 modules + 2 specs + tests.

---

## Recommendation

**Approach A.** It hits all three of the issue's deliverables (3-tier taxonomy, modular tool specs, orchestrator governance) without introducing a parallel registry, and it surfaces the HMAC forgeability gap as a first-class part of the change rather than a hidden footgun. The CLI dispatcher matches the Unix convention the issue's `nora hitl mint` syntax implies; a future `nora hitl revoke` / `nora hitl list` reuses the same seam.

Approach C is the cleanest architectural split, but it doubles the registry surface for a 12-file change — premature. Approach B saves effort but ships a forgeable token and breaks the `nora hitl mint` UX the user wrote.

The **single open question for the proposal phase** is whether the HMAC closure is in-scope for this RFC or deferred to a follow-up. The user's wording ("reuse HMAC validation") suggests they expect HMAC to already exist; this explore surfaces that it does not, and the proposal MUST pick a path.

---

## Risks

* **CRITICAL — HMAC forgeability.** `verify_approval_token` is a stub; `mint` CLI without HMAC ships a forgeable token. The proposal MUST decide whether to add HMAC in this change (Approach A / C) or ship a stub CLI + document the gap (Approach B). If the user believes HMAC already exists, this is the most important correction to surface before the proposal phase writes a single line of spec.
* **CRITICAL — Tool spec front-matter contract is undefined.** Today every `*.md` under `src/nora/prompts/` must match `Prompt-R5` (front-matter with `name` + non-empty `description`). The new `docs/tool_specs/*.md` files need their own contract: at minimum `tool_name` + `tier`; possibly `read_only`, `requires_operator_confirmed`, `requires_hitl_token`. Design phase owns the schema.
* **WARNING — `__main__` refactor ripples.** Refactoring `__main__` from "deprecation alias" to "sub-command dispatcher" touches the back-compat test `test_main_alias.py` and the integration boot test `test_integration_boot.py:323`. Both currently assert the alias delegates to `cli.main()`. The proposal must include a back-compat path (e.g. `nora` with no sub-command → boot MCP, just like today; `nora mcp` and `nora hitl ...` are explicit).
* **WARNING — `FastMCP` tool signature change.** Adding `operator_confirmed: bool = False` to `snmp_run_spectrum_analysis` changes the wire shape (clients that already invoke it without the new arg will still work because it has a default, but operators reading `mcp.list_tools()` will see a new field). The orchestrator prompt change + the typed-exception message are the contract seam.
* **WARNING — Coverage threshold.** `openspec/config.yaml::verify.coverage_threshold = 85`. New code in `spectrum.py`, `exceptions.py`, possibly `tokens.py`, plus the new `ToolSpecRegistry` (if Approach C) all push the covered module count up. TDD + the threshold must hold.
* **SUGGESTION — Tool specs as governance source-of-truth.** Once `docs/tool_specs/` exists, the orchestrator prompt body becomes the LLM-facing summary while each tool spec is the operator-auditable source. The proposal should consider whether the catalog envelope (per-tool index) should gain a `tier` field too, so the boot-time `OidCatalogRegistry` can refuse to expose Tier-2 tools without an HMAC key. Out of scope per the issue but worth flagging.
* **SUGGESTION — Naming consistency.** The user's prompt reads `2026-09-15-3tier-tool-governance`; the issue body describes a "3-tier Service Impact Classification" + "Modular Tool Specifications" + "Orchestrator Governance". All three collapse into `3tier-tool-governance`. Confirmed.
* **SUGGESTION — Read `openspec/specs/nora-mcp-server/spec.md`.** Not opened in explore — the new tool signature + new orchestrator section will likely need a delta in this capability. Proposal phase should re-read it before drafting.

---

## Ready for Proposal

**Yes.** The exploration surfaces enough evidence to write a `proposal.md`:
* Tool list is exact (11 tools + 1 README, no missing entries).
* Tier 0 / Tier 1 / Tier 2 mapping is mechanical.
* Tier-2 infrastructure is already in place (only `mint` CLI is new).
* Tier-1 enforcement point is located and the typed-error pattern is reusable.
* `PromptRegistry` composition has three plausible mechanisms; the design phase will pick one.
* **HMAC forgeability must be flagged in the proposal before anything else.**

The orchestrator MUST surface the HMAC correction explicitly to the user before the proposal phase writes a single line — the user's premise ("HMAC validation already exists in `verify_approval_token`") does not match the code. If the user insists on shipping the stub mint CLI anyway, that is an explicit decision the proposal phase will record as a known limitation.