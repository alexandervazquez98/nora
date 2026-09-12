# Exploration: `2026-09-12-secure-config-reissue`

> Re-issue two contracts accidentally dropped from `secure-configuration/spec.md` during
> the `nora-mcp-thin-split` archive, where the composer’s deterministic
> REMOVED-wins ordering over a `MODIFIED` block erased the surviving intent.
> Date of evidence: branch `main` @ HEAD (post-archive of `nora-mcp-thin-split`).

## Current State

### Specs state (canonical, post-archive)

| Domain | Requirements | Status |
|---|---|---|
| `openspec/specs/secure-configuration/spec.md` | 4 | Two contracts MISSING — `Credentials Never Appear in String Representations`, `Settings Load Status Is Observable`. |
| `openspec/specs/nora-mcp-server/spec.md` | 11 | Owns the *tool-response side* of secret redaction (`Security Boundary — No Secrets in Tool Responses`). Does NOT cover `repr(settings)` or log lines. |

### Implementation state (intact)

`src/nora/config.py` (162 lines, unchanged during thin-split):

- `Settings` (lines 37–73) declares the seven user-settable fields plus the computed `loaded_from` (line 75). The signing key is `SecretStr | None` (line 55), so Pydantic’s default repr masks it as `**********`.
- `LoadSource = Literal[".env", "process_env", "defaults"]` (line 26) — the three sources the missing spec demands.
- `_detect_loaded_from` (lines 78–104) — `mode="before"` validator that tags the source.
- `__init__` (lines 106–138) — resolves `_env_file` then calls `super().__init__()` with `loaded_from` pre-populated.
- `settings_customise_sources` (lines 140–159) — swaps `init_settings, dotenv_settings, env_settings, file_secret_settings` to put `.env` ahead of process env. The spec requires `.env` > process env > defaults.

`src/nora/config.py` does **not** override `__repr__` or `__str__`. Pydantic-Settings 2.x inherits Pydantic’s repr, which uses the masked `SecretStr` form (`**********`). This is the behaviour the missing scenario `repr(settings) masks secrets` will pin.

### Test state (intact)

`tests/test_config.py` (378 lines) covers every scenario that the two missing contracts declare. The tests survived the archive because the thin-split delta did not touch `config.py` or `test_config.py`:

| Test (line) | Pins which missing scenario |
|---|---|
| `test_repr_masks_secret_fields` (198) | `Credentials Never Appear in String Representations > repr(settings) masks secrets` |
| `test_log_lines_never_contain_secrets` (215) | `Credentials Never Appear in String Representations > log lines never contain secrets` |
| `test_env_file_takes_precedence_over_process_env` (233) | `Settings Load Status Is Observable > .env precedence over process environment` |
| `test_process_env_is_used_when_no_env_file` (252) | `Settings Load Status Is Observable > process_env` source branch |
| `test_defaults_are_explicit_when_nothing_is_set` (263) | `Settings Load Status Is Observable > defaults are explicit when nothing is set` |

Verification at HEAD: `uv run python -m pytest tests/test_config.py` → **17 passed in 0.55s**.

### Archive report evidence

`openspec/changes/archive/2026-09-12-nora-mcp-thin-split/archive-report.md` lines 143–172 documents the authoring intent in the delta’s own Reason notes:

- `Credentials Never Appear in String Representations` — “Examples used `GEMINI_API_KEY`; re-issued under MODIFIED with `nora_oid_catalog_signing_key`.”
- `Settings Load Status Is Observable` — “Examples used `NORA_LLM_PROVIDER` and `nora_health`; re-issued under MODIFIED.”

The MODIFIED block (lines 63–100 of the archived delta) carries the intent. The REMOVED block (lines 15–21) was a malformed hand-fix that the composer honoured by deleting the contracts. The re-issue must add the two requirements back as ADDED Requirements in `openspec/changes/2026-09-12-secure-config-reissue/specs/secure-configuration/spec.md`.

### Tool-response side is owned elsewhere

`openspec/specs/nora-mcp-server/spec.md` `Security Boundary — No Secrets in Tool Responses` (lines 63–77) covers the *four tools’* response/error-message contract. The MODIFIED block of `Credentials Never Appear in String Representations` in the archived delta (line 79) included a `Scenario: tool responses never contain secrets`. **This scenario belongs on `nora-mcp-server`, not `secure-configuration`.** The re-issue must therefore *split* the original contract into the right home: `secure-configuration` gets `repr()` and log-line redaction; `nora-mcp-server` already owns the tool-response side. This is also confirmed by the `verifies every response field contains the signing key value` wording in `nora-mcp-server/spec.md:71`.

## Affected Areas

- `openspec/changes/2026-09-12-secure-config-reissue/explore.md` — this file.
- `openspec/changes/2026-09-12-secure-config-reissue/proposal.md` — next phase.
- `openspec/changes/2026-09-12-secure-config-reissue/specs/secure-configuration/spec.md` — written by `sdd-spec`; carries the two ADDED requirements.
- `openspec/specs/secure-configuration/spec.md` — post-archive, target of the two ADDED requirements. Currently 77 lines / 4 requirements; will become ~5 requirements.
- `tests/test_config.py` — already covers all five scenarios; no new tests required.

## Approaches

1. **Pure spec re-issue (RECOMMENDED)** — ADDED Requirements only. No code changes. Tests already pass.
   - Pros: Smallest diff; matches the *authoring intent* recorded in the archive report; `strict_tdd=true` is already satisfied by existing tests.
   - Cons: Splits the original MODIFIED-block scenario “tool responses never contain secrets” into the right home — must be explicit in the ADDED requirement’s wording so we don’t double-state what `nora-mcp-server` already owns.
   - Effort: Low.

2. **Re-issue + add explicit `__repr__` for paranoia** — Define `__repr__` on `Settings` to mask `SecretStr` fields even if Pydantic’s behaviour changes.
   - Pros: Belt-and-suspenders against Pydantic upgrades.
   - Cons: Pure YAGNI — Pydantic-Settings already masks SecretStr; adding an explicit `__repr__` is dead code at the spec level. Pinning the test is enough.
   - Effort: Low (code) + Low (spec) — but spec must justify why we re-implement what Pydantic already does.

3. **Restore as MODIFIED, not ADDED** — Add the contracts as MODIFIED Requirements in the delta, claiming they were on the pre-state spec.
   - Pros: Closer to the archived delta’s MODIFIED-block wording.
   - Cons: Misleading — the contracts are *absent* from the canonical, not present-but-modified. MODIFIED would imply the prior text is being updated; ADDED is honest about the gap.
   - Effort: Low.

## Recommendation

**Approach 1 — Pure spec re-issue as ADDED Requirements**, with these scoping rules:

- Add `Credentials Never Appear in String Representations` (repr + log lines only). Drop the tool-response scenario; that contract lives in `nora-mcp-server/spec.md`.
- Add `Settings Load Status Is Observable` with the `loaded_from` field and the `.env` > process env precedence (already implemented; just contractually re-pinned).
- Word each scenario to match the surviving test exactly: `change-me` literal for repr, `do-not-leak-this-key` for log lines, `NORA_OID_CATALOGS_PATH` for precedence (the tests use these — pinning to the test wording makes the spec the contract, not a wishlist).

## Risks

- **Scenarios duplicate `nora-mcp-server`'s secret contract.** If the re-issued requirement retains the “tool responses never contain secrets” scenario, both specs will assert the same behaviour and one will drift. Mitigation: omit that scenario; the boundary is documented in `nora-mcp-server/spec.md` and cross-referenced from `secure-configuration/spec.md` `## Cross-References` (currently absent; add it).
- **Modular lint failure.** `ruff` and `mypy` are clean at HEAD; an ADDED Requirements block has no code impact, so the build gates are inert.
- **No implementation drift.** The two requirements are pinned by tests that already pass at HEAD; no code work is needed.
- **Re-introducing the REMOVED+MODIFIED pitfall.** The composer will accept the ADDED block; if a future author naively edits the existing 4 requirements’ content they may again be tempted to double-list. Mitigation: this change is named `…-reissue` and the proposal explicitly forbids touching the MODIFIED/REMOVED blocks.

## Ready for Proposal

Yes. The change is small, mechanical, and pure spec work. The proposal should call out:

- Two ADDED Requirements (one for `repr()`/log redaction; one for `loaded_from` observability).
- Cross-reference added to `secure-configuration/spec.md` `## Cross-References`.
- No code changes. No new tests (existing tests pin every scenario).
- Risk: future authors must NOT add a third “tool responses never contain secrets” scenario here; it would duplicate `nora-mcp-server/spec.md`.

Next phase: `sdd-propose` (this turn) → `sdd-spec` (single delta) → `sdd-apply` (no-op, just `sdd-archive-compose`) → archive.
