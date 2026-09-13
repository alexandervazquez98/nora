# Proposal: Intervention Memory Writer Contract (Save Tool)

> **SCOPE.md phase:** **Fase 4 (Interfaces FastMCP)**. Dependency-enabler for Fase 3's HITL Controller; no device writes. Rollback in §"Rollback Plan"; rejects autonomous mutation (HITL lives in #15).

## Intent

NORA exposes `save_intervention_record` — a 5th `@mcp.tool` that atomically writes one JSON record under `Settings.nora_interventions_dir`. Writer lives in NEW sibling `src/nora/intervention_writer/`, so `nora.intervention_memory` keeps **R2 hard-read-only at 100%** (issue #12). Pure logic in `writer.py`; MCP wrapper in `src/nora/server.py`.

## Scope

**In Scope.** New `src/nora/intervention_writer/` (3 modules); 5th `@mcp.tool` in `src/nora/server.py` (~25 LOC); atomic write `tmp + fsync + os.replace`; sanitization-on-write via `Sanitizer.sanitize(...)`; path-traversal guard (`[A-Za-z0-9_-]+` + `Path.resolve()`); schema validation via `model_validate(...)`; 4 test files + 1 server scenario.

**Out of Scope.** HITL gate (record-keeping; #15 owns HITL); v7→v8 migration; webui.db mirror shim; multi-tenant; remote transports.

## Capabilities

### New
- **`intervention-writer`** — filename template, path-traversal guard, atomic write, sanitization-on-write, schema validation, MCP wrapper shape, banned-imports boundary, one-way dep `writer → reader.models`. Spec at `openspec/changes/2026-09-13-intervention-memory-writer-contract/specs/intervention-writer/spec.md` → `openspec/specs/intervention-writer/spec.md` at archive.

### Modified
- **`intervention-memory`** — `__init__.py` docstring addendum; R2 stays.
- **`nora-mcp-server`** — `ADDED` scenario for 5th `@mcp.tool`; references `intervention-writer`.

## Affected Areas

| Area | Impact |
|------|--------|
| `src/nora/intervention_writer/` | New |
| `src/nora/server.py` | Modified |
| `src/nora/intervention_memory/__init__.py` | Modified |
| `tests/intervention_writer/` | New |
| `tests/test_server.py` | Modified |
| `openspec/specs/intervention-writer/spec.md` | New |
| `openspec/specs/intervention-memory/spec.md` | Modified |
| `openspec/specs/nora-mcp-server/spec.md` | Modified |

## Risks

| Risk | Lik | Mitigation |
|------|-----|------------|
| Filename collision w/ OpenChat | Low | Distinct prefix; `secrets.token_hex(3)` retry x5 |
| Torn `.tmp` on SIGKILL | Med | `tmp + fsync + os.replace`; reader skips `.tmp` |
| Path-traversal | Med | Strict regex + `Path.resolve()` containment |
| Oversize payload | Med | Pydantic parse cost; design may add byte cap |
| Schema drift | Low | One-way dep writer → reader.models |

## Rollback Plan

1. Remove `@mcp.tool` registration in `src/nora/server.py` (~3 LOC).
2. Optionally drop `src/nora/intervention_writer/` + `tests/intervention_writer/`.
3. **On-disk records remain valid**: writer refuses overwrite (`DUPLICATE_INTERVENTION_ID`); reader's `*.json` glob unaffected; R2 guard stays at 100%.

## Dependencies

- **Internal**: `nora.intervention_memory.models` (one-way), `nora.config.Settings`, `Sanitizer`.
- **Stdlib**: `json`, `os`, `pathlib`, `secrets`, `tempfile`. No new dep.

## Success Criteria

- [ ] `.venv/bin/python -m pytest` exits 0 (4 new test files).
- [ ] `tests/intervention_memory/test_no_writes.py` passes — R2 guard untouched.
- [ ] Path-traversal: `INT-../../../etc/passwd-1-123-XYZ` → `PATH_TRAVERSAL_DETECTED`.
- [ ] Atomic-write: SIGKILL leaves `*.json.tmp` only; reader skips; next writer call sweeps.
- [ ] Sanitization-on-write: private IPv4 → on-disk file has `RADIO_NODE_*` alias, not literal.
- [ ] Coverage on `src/nora/intervention_writer/` ≥85%.
- [ ] `ruff check . && ruff format --check . && mypy --strict src/nora` exit 0.