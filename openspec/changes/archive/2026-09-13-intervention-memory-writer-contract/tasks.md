# Tasks: Intervention Memory Writer Contract (Save Tool)

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | ~830 total (single PR) |
| 400-line budget risk | High |
| Chained PRs recommended | No (user accepted size:exception) |
| Suggested split | Single PR — user-approved size:exception |
| Delivery strategy | exception-ok |
| Chain strategy | size-exception |
| Decision needed before apply | No (maintainer-approved) |

Decision needed before apply: No
Chained PRs recommended: No
Chain strategy: size-exception
400-line budget risk: High

### Suggested Work Units

| Unit | Goal | Likely PR | Focused test command | Runtime harness | Rollback boundary |
|------|------|-----------|----------------------|-----------------|-------------------|
| 1 | writer package + 4 test files + MCP wiring + 6 test-file bumps + R2 cross-ref | PR 1 (single) | `uv run python -m pytest tests/intervention_writer/ tests/{test_server,test_integration,test_integration_boot,test_main_alias,test_prompts,intervention_memory/test_no_writes}.py tests/installer/test_verify_install.py -x` | `uv run python -m nora` over stdio (smoke) | Revert single commit; reader R2 untouched |

## Phase 1: Filenames — PR 1

- [x] 1.1 RED: `tests/intervention_writer/test_filename_helpers.py` asserts `INT-TKT-001-10.0.0.1-1700000000-a1b2c3.json`.
- [x] 1.2 RED: extend with `..`, `/`, NUL, unicode, whitespace — `InvalidFilenameComponent`.
- [x] 1.3 GREEN: create `src/nora/intervention_writer/__init__.py` (docstring + re-export).
- [x] 1.4 GREEN: create `src/nora/intervention_writer/filenames.py` (`InvalidFilenameComponent` + `build_filename`; regex `^[A-Za-z0-9_-]+$`).
- [x] 1.5 REFACTOR: hoist `_SAFE_COMPONENT_RE`.

## Phase 2: Atomic Write + Sweep — PR 1

- [x] 2.1 RED: `tests/intervention_writer/test_atomic_write.py` — success: only `.json`, no `.tmp`.
- [x] 2.2 RED: pre-create `INT-X.json.tmp` 90 min old; next call sweeps.
- [x] 2.3 RED: symlink `escape_link -> /tmp` → `PATH_TRAVERSAL_DETECTED`.
- [x] 2.4 GREEN: in `src/nora/intervention_writer/writer.py` add `_atomic_write` + `_sweep_stale_tmp`.
- [x] 2.5 REFACTOR: type hints + parametrize.

## Phase 3: Schema + Sanitization-on-Write — PR 1

- [x] 3.1 RED: `tests/intervention_writer/test_writer_contract.py` — valid payload → `OK` + `intervention_id` stem.
- [x] 3.2 RED: `stage="PRE_MIGRATIO"` → `INVALID_PAYLOAD`, no disk write.
- [x] 3.3 RED: `findings_and_dictamen` with `"192.168.1.1"` lands as `RADIO_NODE_X` (W4).
- [x] 3.4 RED: `ticket_number`/`target_ip` byte-identical post-sanitize (R9 bypass).
- [x] 3.5 GREEN: implement `save_intervention_record` in `src/nora/intervention_writer/writer.py` (validate→sanitize→build→resolve→sweep→atomic; never raises).
- [x] 3.6 REFACTOR: extract `_sanitize_payload`; reuse `_BYPASS_FIELDS`.

## Phase 4: Banned-Imports AST Guard — PR 1

- [x] 4.1 RED: `tests/intervention_writer/test_banned_imports.py` AST-walks `src/nora/intervention_writer/*.py`; fails on 6 banned imports.
- [x] 4.2 GREEN: scan; baseline passes.
- [x] 4.3 REFACTOR: poison self-test — inject `import nora.server`; assert caught.

## Phase 5: MCP Wiring + Reader Cross-Ref — PR 2

> **Design gap.** 6 test files hardcode 4-tool set; 5.2 lists.

- [x] 5.1 RED: in `tests/test_server.py` rename `test_server_exposes_exactly_four_tools` → `_five_tools`; add 5th name.
- [x] 5.2 RED: same 1-line update in `tests/test_integration_boot.py`, `test_integration.py`, `test_main_alias.py`, `test_prompts.py`, `installer/test_verify_install.py`.
- [x] 5.3 GREEN: edit `src/nora/server.py`: 5th `@mcp.tool` + `__all__` + `_SERVER_INSTRUCTIONS`.
- [x] 5.4 GREEN: edit `src/nora/intervention_memory/__init__.py`: cross-referencing addendum (R2 stays).
- [x] 5.5 GREEN: add `test_no_writes.py::test_reader_does_not_import_from_writer_sibling`.

## Phase 6: Verify

- [x] 6.1 pytest exits 0; writer coverage ≥85%.
- [x] 6.2 ruff check + format + mypy exit 0.
- [x] 6.3 W6 spot-check: stdio with `NORA_INTERVENTIONS_DIR=/tmp/x` lands.
