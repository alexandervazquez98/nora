# Design: 3-Tier Tool Service-Impact Governance (issue #43)

## Context

Issue #43 mandates a 3-tier Service-Impact taxonomy for NORA's 12 MCP
tools, modular per-tool specs at `docs/tool_specs/`, and deterministic
server-side guards: Tier 1 requires `operator_confirmed=True`; Tier 2
requires an HMAC-signed HITL token minted via `nora hitl mint`.

**Goals (Scope B+):** 3-tier taxonomy; tool-spec multi-dir scan;
Tier-1 server-side gate before any SNMP GET; Tier-2 HMAC forgeability
closure; `nora hitl mint` operator CLI; orchestrator prompt §6 Universal
Service Impact & Disruption Gate.

**Non-Goals:** Open WebUI changes; Phase-3 ChangeRequest cluster; key
rotation; tool-spec schemas beyond the 11 listed.

## Architecture Decisions

### ADR-1 — Prompt composition: inline marker, scan-time (Q1)

**Picked:** inline marker (`<!-- tool_spec: name -->`) resolved at scan
time; `get("netops_orchestrator")` returns the composed body. **Rejected:**
runtime name-lookup at `@mcp.prompt` call time (mutable seam, breaks
R1). `from_settings` scans `_PACKAGED_PROMPTS_DIR` and
`settings.nora_tool_specs_dir`; markers substitute matched tool-spec
bodies at scan time; unmatched markers log a warning.

### ADR-2 — Legacy-token back-compat: hard-break (Q2)

**Picked:** hard-break. Closes forgeability; release notes + kill
switch (`NORA_HITL_TOKEN_TTL_SECONDS=0`) as backout. **Rejected:**
dual-verify (re-opens forgeability); `version: 2` field (HMAC proves
key-validity).

### ADR-3 — CLI dispatcher: `__main__.py` sub-parser (Q3)

**Picked:** argparse sub-parser; no new console script. Single CLI
surface; mirrors Unix `git/cargo`; preserves `nora` no-args
back-compat. **Rejected:** new `nora-hitl` entry (Q3 spec); Typer
(breaks pattern); hand-rolled.

Refactored `__main__.py` (~80 LOC): `argv[1]` ∈ {`mcp`, `hitl`, None,
other}. `nora hitl mint` parses `--operator-id` / `--ttl-seconds`.
`nora` (no args) emits `DeprecationWarning` then calls `cli.main()`
(back-compat pin). Unknown → help to stderr + exit 2. `pyproject.toml`
unchanged.

### ADR-4 — Tool-spec front-matter schema (Q4 — CRITICAL)

**Picked: option (b).** Frozen contract:

```yaml
name: <tool_name>                # MUST match filename stem
description: <non-empty>
tier: 0 | 1 | 2                  # required
requires_operator_confirmed: bool # required when tier == 1
requires_hitl_token: bool         # required when tier == 2
```

Validator invariants: `tier ∈ {0,1,2}` (else `PromptNotFoundError`);
tier 1 ⇒ `requires_operator_confirmed: True`; tier 2 ⇒
`requires_hitl_token: True`; tier 0 MAY have both `false`. README.md
(no `tier`) scanned but carries no contract. Option (a) tier-alone is
implicit; option (c) `prerequisites: list[str]` is stringly-typed.

### ADR-5 — Signing key: Settings `SecretStr`, lazy fail-closed (Q5)

`Settings.nora_hitl_signing_key: SecretStr` mirrors catalog key; loaded
from `NORA_HITL_SIGNING_KEY` env / `.env`. **Lazy fail-closed**: empty
key at first Tier-2 invocation → `AutonomousMutationRejected` literal;
Tier-0/Tier-1 boots proceed. **Rotation out of scope** (Phase-3).

**Divergence from `OidCatalogRegistry`:** catalog HMAC fails at boot
(build-time artefact); HITL HMAC fails at first Tier-2 invocation
(operator session determines Tier-2 use). Both use `SecretStr`; trigger
differs.

### ADR-6 — HMAC canonical payload (frozen)

```
f"{operator_id}|{issued_at_iso8601}|{expires_at_iso8601}|{token}"
```

UTF-8 → `hmac.new(key_bytes, body, hashlib.sha256).hexdigest()` → verify
with `hmac.compare_digest`. Tuple not JSON dict (no canonicalization
drift). `HitlApprovalToken.signature: str = Field(min_length=1)`; legacy
stub tokens (no `signature`) fail Pydantic → `AutonomousMutationRejected`
literal.

### ADR-7 — Coverage & line budget (Q6)

**Forecast:** code + tests ≈ 305 LOC (under 400-line review budget).
Adding 12 new `docs/tool_specs/*.md` (~380 LOC) tips total over 400 —
**docs growth, not code growth; flagged for review approval**.

Key deltas: `cli_hitl.py` (+90, create); `__main__.py` (+50,
dispatcher); `tokens.py` (+60, HMAC); `registry.py` (+55, multi-dir);
`config.py` (+20); `exceptions.py` (+15); `server.py` (+5); orchestrator
prompt (+45); `cli.py` / `.env.example` (+5); 5 test files (+60); 12
docs/tool_specs (+380, not in coverage). `cli_hitl.py` and HMAC branches
100% hermetic-test-covered; apply phase refuses merge below 85%.

### ADR-8 — Capability reconciliation

**Keep `secure-configuration` in-scope.** HMAC requires
`Settings.nora_hitl_signing_key`; `secure-configuration` R1 says
"Pydantic Settings is the only configuration source" — moving the key
outside Settings violates R1 and breaks
`test_no_os_environ_in_src_nora`. Net: 6 capabilities (4 modified + 2
new) is correct.

## Data Model & Wire-Shape

```python
class HitlApprovalToken(BaseModel):  # frozen; signature NEW
    token: str = Field(min_length=1)
    operator_id: str = Field(min_length=1)
    issued_at: datetime
    expires_at: datetime
    signature: str = Field(min_length=1)

class Settings(BaseSettings):  # two new fields
    nora_hitl_signing_key: SecretStr | None = None
    nora_tool_specs_dir: Path | None = Path("docs/tool_specs")

class Tier1ClearanceRequired(DriverError):  # NEW
    """Tier 1 tool invoked without operator_confirmed=True."""
```

**FastMCP `snmp_run_spectrum_analysis`:**
`{"device_id": string, "operator_confirmed": boolean (default false)}`.
**CLI `nora hitl mint`:** stdout = one JSON line `{"token",
"operator_id", "issued_at", "expires_at", "signature"}`. Exit 0
success; 2 bad args.

## Migration / Rollout

Release notes warn pre-deploy stub tokens are invalidated; kill switch
(`NORA_HITL_TOKEN_TTL_SECONDS=0`) is the backout. Empty
`nora_hitl_signing_key` is NOT boot-fatal (Tier-0/1 proceed); empty
catalog key STILL aborts boot (existing R3). Backout: HMAC → CLI →
tool-spec → Tier-1 gate; each `git revert`-able.

## Risks

| ID | Sev | Risk | Mitigation |
|---|---|---|---|
| R1 | CRITICAL | Q4 schema freeze mismatch — 12 docs re-touched. | ADR-4 freezes schema. |
| R2 | WARN | CLI dispatcher breaks back-compat tests. | ADR-3 keeps no-args deprecation-alias. |
| R3 | WARN | Coverage threshold — new code increases denominator. | ADR-7 enumerates; hermetic tests. |
| R4 | WARN | Spectrum wire-shape change — typed exception to existing clients. | Orchestrator §6 negotiates clearance. |
| R5 | WARN | Code+tests ~305 LOC; +12 docs tips total over 400. | Docs are the deliverable; flagged. |
| R6 | SUGG | Key rotation deferred. | Phase-3 follow-up. |

## Threat Matrix

**N/A** — no routing, shell exec, subprocess, VCS automation, or
process-integration changes. CLI refactor is Python argparse.
`hmac.compare_digest` is timing-safe (non-negotiable per ADR-6).

## Open Questions for Tasks

- Extend `test_cli.py` vs new `tests/test_cli_hitl.py`. Recommended:
  extend `test_cli.py`.
- Apply phase authors the 12 `docs/tool_specs/*.md` against ADR-4.
- Verify `docs/` not in coverage denominator (apply phase sanity check).
