# Proposal: nora-mcp-thin-split

## Intent

NORA's 9-tool MCP carries `nora_health`, 4 `nora_session_*` tools, and an LLM block serving a workflow openchat UIWeb owns client-side (explore.md L47-74). Split into a thin `nora-mcp` exposing only the 1 driver + 3 intervention tools openchat calls, dropping 9 Settings fields, 2 SDKs, and ~1100 dead lines.

## Scope

**In:** Per explore.md classification (L106-135). Trim Settings 16→7; delete `llm.py`, `core/*`. Strip `server.py` (drop `nora_health`, 4 `nora_session_*`, `_AutoTraceMiddleware`, LLM state, journal re-export). Cut `drivers/snmp_pmp450i/driver.py:44`+`:115`. Add `cli.py`; 12-line `__main__.py`→`cli.main`. `pyproject.toml`: add `nora-mcp`, drop `lmstudio`, `google-genai`. Regenerate `.env.example` to 7 keys. Tests: 13 deleted, 5 migrated.

**Out:** `nora-service` binary (locked out). New tools, new vendors, intervention-write. Schema changes to surviving tools. `shim_webui.py` unchanged.

## Capabilities

**New:** None. **Modified:** `nora-mcp-server` (REMOVE `nora_health` req; ADD `python -m nora` deprecation); `secure-configuration` (MODIFY Settings field list to 7). **Archived:** `session-journal`; `llm-provider-interface` (openchat owns the LLM).

## Approach

Single PR, big-bang (locked decision 4). Shape: `cli.py` + deprecation `__main__.py`. **Cut** `llm.py`+`core/*`, `config.py` LLM/journal blocks, `server.py` LLM state + tools + middleware + `__all__`, `drivers/snmp_pmp450i/driver.py` seam. **Rewire** `cli.py` + 12-line alias `__main__.py` → `cli.main`; `pyproject.toml` scripts. **Trim** tests, `lmstudio` + `google-genai`, `.env.example`.

## Risks

| Risk | Lik | Mitigation |
|------|-----|------------|
| Dead symbols: `_DEFAULT_OPERATOR_ALIAS` + `ProviderName` lose all callers | Med | Delete with the cut; mypy --strict confirms no residual refs. |
| `hermetic_settings` (conftest.py L151-162) passes trimmed Settings kwargs to `test_server.py` + `test_server_driver_tool.py` | Med | Update fixture first; both consumers are on the migration list. |
| Undetected external `nora_health` consumer (only repo-wide grep) | Med | Locked decision 3 is explicit; surface the break in the PR description so openchat sees it pre-merge. |
| 4 driver tests monkeypatch the deleted `nora_session_set_focus` symbol | High | Migration removes the patches; no assertion depends on focus being set. |

## Rollback Plan

Revert the merge commit. No data migrations (orphaned journal NDJSON on operator disks is non-destructive). Operators fall back to the previous tag's `nora` script.

## Success Criteria

- [ ] `nora-mcp` boots, exposes exactly 4 tools (`snmp_get_pmp450i_radio_metrics` + 3 intervention tools).
- [ ] `python -m nora` emits `DeprecationWarning` and boots the identical surface.
- [ ] Surviving tests pass under `uv run python -m pytest --cov=src/nora --cov-report=term-missing`; deleted tests leave no stale imports.
- [ ] `uv run mypy --strict src/nora` green.
- [ ] `uv run ruff check .` and `uv run ruff format --check .` clean.
- [ ] `pyproject.toml` no longer lists `lmstudio` or `google-genai`.
- [ ] `.env.example` ≤ 20 lines, lists every surviving Settings field exactly once.

## Approval Path

No gates between `sdd-propose` and `sdd-spec`. Specs writes deltas for `nora-mcp-server` + `secure-configuration` and empty-REMOVED deltas for `session-journal` + `llm-provider-interface`.