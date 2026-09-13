# Design: Intervention Memory Writer Contract (Save Tool)

## Technical Approach

Writer in a **sibling package** `src/nora/intervention_writer/` so reader R2 stays at 100%. Pure logic in `writer.py`; 5th `@mcp.tool` in `src/nora/server.py`. One-way dep `writer → reader.models + reader.sanitize`; reader → writer is structurally impossible.

## Architecture Decisions

| Decision | Choice | Alternatives | Rationale |
|----------|--------|--------------|-----------|
| Package shape | New sibling `src/nora/intervention_writer/` | (a) writer in `server.py`; (b) writer inside reader with whitelist | Sibling costs ~30 LOC but makes read-only regression physically impossible. |
| MCP wrapper location | 5th `@mcp.tool` in `src/nora/server.py` | `mcp_bridge.py` inside writer package | `server.py` already owns `_ToolLogMiddleware` + `_SERVER_INSTRUCTIONS`; one file audits the FastMCP surface. Library stays callable from non-MCP code (issue #15 reuse). |
| Filename template | `INT-<ticket>-<ip>-<unix>-<6hex>.json`; `[A-Za-z0-9_-]+` on ticket/ip; 6-hex `secrets.token_hex(3)`; **5-retry** budget | UUID6 | `INT-` prefix avoids OpenChat collision; 16⁶ suffixes make same-(ticket,ip,unix) collision vanishingly small. Reader `*.json` glob is prefix-agnostic. |
| Atomic write | `tmp = target.with_suffix('.json.tmp'); write_text; fsync; os.replace`. Per-call sweep of `*.json.tmp` older than 3600 s | direct `write_text` | `os.replace` atomic on POSIX/macOS; `.tmp` suffix makes reader glob skip torn files; sweep handles SIGKILL window. |
| Sanitization | Reuse `nora.intervention_memory.sanitize._sanitize_value` over validated payload BEFORE serialisation | sanitize on read only | Defense in depth: credential paste lands masked on disk; future reader bug still sees safe file. |
| Payload size cap | **Defer** `nora_interventions_max_record_bytes` to v2 | Add now | Issue #12 doesn't ask for it; schema + regex + `Path.resolve()` already bound the writer. |
| R-NEW-1 vs R-NEW-5 | Apply updates 6 test files' four-tool → five-tool assertions. Do NOT MODIFY main R-NEW-1 here. | MODIFY R-NEW-1 in this delta | R-NEW-1 is shipped baseline; follow-up delta bumps it. |

## Data Flow

```
MCP client ─JSON-RPC─▶ mcp.save_intervention_record(payload)  [5th @mcp.tool]
                          ▼
        writer.save_intervention_record(settings, payload)
                          │ 1. model_validate(payload)         [W5]
                          │ 2. _sanitize_value(model_dump, Sanitizer) [W4]
                          │ 3. build_filename(payload)          [W1]
                          │ 4. target.resolve().is_relative_to(base) [W2]
                          │ 5. sweep *.json.tmp older than 3600 s    [W3]
                          │ 6. tmp.write_text → fsync → os.replace   [W3]
                          ▼
                  nora_interventions_dir/*.json
```

`_ToolLogMiddleware` emits one stderr line per call.

## File Changes

| File | Action | Description |
|------|--------|-------------|
| `src/nora/intervention_writer/__init__.py` | Create | Docstring declaring writer invariant (mirror reader). Re-export `save_intervention_record`. |
| `src/nora/intervention_writer/writer.py` | Create | `save_intervention_record(settings, payload) -> dict`. Steps 1-6. Returns `"OK"` / `"INVALID_PAYLOAD"` / `"PATH_TRAVERSAL_DETECTED"` / `"DUPLICATE_INTERVENTION_ID"` / `"WRITE_ERROR"`. Never raises. |
| `src/nora/intervention_writer/filenames.py` | Create | `build_filename(payload, unix, hex_suffix) -> str`. Regex `[A-Za-z0-9_-]+`; raises `InvalidFilenameComponent`. |
| `src/nora/server.py` | Modify | +1 import + 5th `@mcp.tool` (~25 LOC). Add to `__all__`. Append to `_SERVER_INSTRUCTIONS`. |
| `src/nora/intervention_memory/__init__.py` | Modify | Addendum: cross-reference + one-way dep. R2 wording unchanged. |
| `tests/intervention_writer/test_writer_contract.py` | Create | W5 valid writes; W5 invalid `stage` → `INVALID_PAYLOAD`; W4 IPv4 masked on disk; W4 bypass scalars byte-identical. |
| `tests/intervention_writer/test_filename_helpers.py` | Create | W1/W2 regex: reject `..`, `/`, NUL, whitespace, unicode. |
| `tests/intervention_writer/test_atomic_write.py` | Create | W3 atomic write; SIGKILL leaves `.tmp`; next call sweeps; symlink-escape refused. |
| `tests/intervention_writer/test_banned_imports.py` | Create | W7 AST scan — zero matches against six banned names. Poison self-test. |
| `tests/test_server.py`, `tests/test_integration.py`, `tests/test_integration_boot.py`, `tests/test_main_alias.py`, `tests/test_prompts.py`, `tests/installer/test_verify_install.py` | Modify | Bump four-tool assertion → five-tool; expected set adds `save_intervention_record`. Six files total. |
| `tests/intervention_memory/test_no_writes.py` | Modify | Add `test_reader_does_not_import_from_writer_sibling`. Existing R2 AST scan untouched. |

## Interfaces / Contracts

```python
# writer.py
def save_intervention_record(settings: Settings, payload: dict[str, Any]) -> dict[str, Any]:
    """Atomic-write one record. Never raises."""

# filenames.py
def build_filename(payload: dict[str, Any], unix: int, hex_suffix: str) -> str:
    """Raises InvalidFilenameComponent on violation."""
```

## Testing Strategy

| Layer | What to Test | Approach |
|-------|-------------|----------|
| Unit | W1-W5 (regex, containment, atomic, sanitize, schema) | `tests/intervention_writer/test_*` — pure + `tmp_path`, no MCP. |
| Unit | W7 banned imports | AST scan + poison self-test. |
| Integration | W6 stdio + middleware log | `tests/test_server.py` — `Client(server_mod.mcp).call_tool(...)`. |
| Read-side invariant | R2 reader stays read-only; reader never imports writer | Extend AST scan; write-call scan untouched. |
| Coverage | ≥ 85% on `src/nora/intervention_writer/` | `uv run python -m pytest --cov=src/nora/intervention_writer --cov-report=term-missing`. |

## Threat Matrix

N/A — no routing, shell, subprocess, VCS/PR automation, executable-file classification, or process-integration boundary. Writer is sync stdlib-only (`json`, `os`, `pathlib`, `secrets`, `tempfile`); no `subprocess`, no `shutil`, no network I/O.

## Migration / Rollout

No migration required. OpenChat records (`{ticket}_{ip}_{stage}_{timestamp}.json`) still read by reader's prefix-agnostic glob; new NORA writes use `INT-` prefix. **Rollback**: remove 5th `@mcp.tool` (~3 LOC); on-disk records remain valid.

## Open Questions

- [ ] **`nora_interventions_max_record_bytes`** — deferred to v2 (orthogonal to W1-W8).
- [ ] **R-NEW-1 post-archive MODIFY** — main spec still says "exactly four"; follow-up delta bumps to "five".
- [ ] **Transport** — NORA boots stdio today; library function is transport-agnostic. Out of scope.

