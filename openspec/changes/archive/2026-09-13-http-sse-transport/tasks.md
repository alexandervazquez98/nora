# Tasks: HTTP/SSE Transport for `nora-mcp`

> **Single PR** (forecast 240–365 lines, well below 800-line budget). Test runner `uv run python -m pytest --cov=src/nora --cov-report=term-missing`; coverage threshold 85%. Strict TDD (RED → GREEN) inside every slice. Issue #34.

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | 240–365 |
| 800-line budget risk | Low |
| Chained PRs recommended | No |
| Delivery strategy | ask-on-risk |
| Chain strategy | single-pr |
| Decision needed before apply | No |

Decision needed before apply: No
Chained PRs recommended: No
Chain strategy: single-pr
800-line budget risk: Low

### Suggested Work Units

| Unit | Goal | Likely PR | Focused test command | Runtime harness | Rollback boundary |
|------|------|-----------|----------------------|-----------------|-------------------|
| 1 | Transport-configurable `nora-mcp` boot + installer wiring | PR 1 (single) | `uv run pytest tests/test_cli.py tests/test_http_transport_smoke.py` | `nora-mcp --transport=http --port=8765` then `httpx.get("/mcp")` | Revert merge; remove `EnvironmentFile=/etc/nora/nora-mcp.env` line; `daemon-reload` + `restart` |

---

## Task 1: RED scaffold for transport-aware `cli.main` + HTTP smoke

Write the failing tests FIRST. They MUST fail against the current `cli.py` (stdio hardcode at line 78).

- [x] 1.1 `tests/test_cli.py` — replace `test_cli_main_invokes_mcp_run_without_transport_arg` (lines 47–58) with 6 tests: `test_default_stdio_invokes_mcp_run_with_transport_stdio`; `test_env_vars_select_transport_when_no_cli_flag`; `test_cli_flags_override_env_vars`; `test_invalid_transport_exits_2_with_stderr_naming_options`; `test_stateless_http_with_sse_rejected`; `test_help_exits_zero_lists_transport_flag`. Use `monkeypatch.setattr(cli.mcp, "run", mock)`; `capsys`; `monkeypatch.setenv`.
- [x] 1.2 `tests/test_http_transport_smoke.py` — NEW. Subprocess-boot `nora-mcp --transport=http --host=127.0.0.1 --port=8765 --path=/mcp`; `httpx.get("http://127.0.0.1:8765/mcp")` returns non-zero status (any HTTP response = bind proven); teardown kills child + waits.
- [x] 1.3 Verify RED: `uv run pytest tests/test_cli.py tests/test_http_transport_smoke.py -x` exits non-zero with 7 expected failures (6 in `test_cli.py` + 1 in `test_http_transport_smoke.py`); NOT silent skip.

Commit: `test(cli): RED scaffold for configurable transport + http smoke`

---

## Task 2: GREEN `cli.py` transport dispatch

Make the failing tests pass.

- [x] 2.1 `src/nora/cli.py` — add `VALID_TRANSPORTS` frozenset; `@dataclass(frozen=True) TransportConfig`; `_build_parser` (prog=`nora-mcp`, `--transport` with `choices=VALID_TRANSPORTS`, `--host`, `--port` int, `--path`, `--stateless-http/--no-stateless-http`); `_parse_transport_args(argv)`; `_resolve_transport_config(args)` (CLI > env > defaults); `_validate_transport(config)` (raises `ValueError` on bad value OR `sse+stateless_http=True`).
- [x] 2.2 `src/nora/cli.py` — change `main()` signature to `main(argv: Sequence[str] | None = None)`; route CLI args through resolution + validation; on `ValueError`, `sys.stderr.write(f"nora-mcp: {exc}\n"); sys.exit(2)` BEFORE any `mcp.run`; replace `mcp.run(show_banner=False)` at line 78 with `mcp.run(transport=config.transport, host=config.host, port=config.port, path=config.path, stateless_http=config.stateless_http, show_banner=False)`. **Preserve** the `verify_tools_are_catalogued(catalog_registry)` ordering at line 66 (catalog guard runs BEFORE transport kickoff).
- [x] 2.3 `src/nora/cli.py` — module docstring lines 15–18: drop "stdio-only" assertion; rewrite to "Default `stdio`. Override via `NORA_MCP_TRANSPORT` env var or `--transport` CLI flag."
- [x] 2.4 Verify GREEN: `uv run pytest tests/test_cli.py -x` exits 0; `uv run ruff check src/nora/cli.py` exits 0; `uv run mypy --strict src/nora/cli.py` exits 0; coverage `src/nora/cli.py` ≥95%.

Commit: `feat(cli): configurable transport via NORA_MCP_* env vars + --transport flag`

---

## Task 3: `.env.mcp.example` template

- [x] 3.1 NEW `.env.mcp.example` at repo root — template from `design.md` lines 84–94: comment header explaining `systemd` reload semantics, `NORA_MCP_TRANSPORT=stdio` (default), four commented-out vars (`_HOST=127.0.0.1`, `_PORT=8005`, `_PATH=/mcp`, `_STATELESS_HTTP=false`). Mode 0644 in repo.
- [x] 3.2 Verify: `test -f .env.mcp.example && grep -q '^NORA_MCP_TRANSPORT=stdio' .env.mcp.example` exits 0.

Commit: `chore(env): ship .env.mcp.example template for nora-mcp.env`

---

## Task 4: `install.sh::phase_env_file_mcp()` + `--force-transport-env` flag

New function — do NOT extend `phase_env_file()` (signing-key injection must stay centralized for `nora.env`).

- [x] 4.1 `scripts/install.sh` — add `FORCE_TRANSPORT_ENV=0` default near line 78; `--force-transport-env)` parse case near line 200; usage entry near line 162.
- [x] 4.2 `scripts/install.sh` — NEW `phase_env_file_mcp()` after line 367: mirrors `phase_env_file()` install-line idiom but sources from `${PREFIX}/.env.mcp.example` to `${CONFIG_DIR}/nora-mcp.env` at mode 0640 `nora:nora`; gated by `FORCE_TRANSPORT_ENV` + file-exists check; NO signing-key injection.
- [x] 4.3 `scripts/install.sh` — invoke `phase_env_file_mcp` from `main()` after `phase_env_file` (preserves order: signing-key first, transport second).
- [x] 4.4 Verify: `bash -n scripts/install.sh` exits 0; if `tests/installer/test_install.py` AST-walks `install.sh`, the new function is parseable.

Commit: `feat(installer): phase_env_file_mcp writes /etc/nora/nora-mcp.env`

---

## Task 5: `bootstrap.sh` flag forwarding

- [x] 5.1 `scripts/bootstrap.sh` — add `FORCE_TRANSPORT_ENV="false"` default near line 72; `--force-transport-env)` case near line 122 (sets `FORCE_TRANSPORT_ENV="true"`).
- [x] 5.2 `scripts/bootstrap.sh` — forward in `install_args` near line 265: `if [[ "${FORCE_TRANSPORT_ENV}" == "true" ]]; then install_args+=(--force-transport-env); fi`.
- [x] 5.3 Verify: `bash -n scripts/bootstrap.sh` exits 0.

Commit: `feat(installer): bootstrap.sh forwards --force-transport-env`

---

## Task 6: systemd unit second `EnvironmentFile=`

- [x] 6.1 `scripts/nora-mcp.service` — after line 26 (`EnvironmentFile=/etc/nora/nora.env`), add `EnvironmentFile=/etc/nora/nora-mcp.env` on its own line. Insert a comment explaining env-only changes don't need `daemon-reload`.
- [x] 6.2 Verify: `grep -c '^EnvironmentFile=/etc/nora' scripts/nora-mcp.service` prints `2`.

Commit: `feat(systemd): load /etc/nora/nora-mcp.env on nora-mcp.service start`

---

## Task 7: `verify-install.sh::check_http_listener()` + `--check-http` flag

Opt-in (default off). Mirrors `--skip-functional` opt-out pattern.

- [x] 7.1 `scripts/verify-install.sh` — add `CHECK_HTTP=0` default near line 71; `--check-http)` case near line 195; usage entry.
- [x] 7.2 `scripts/verify-install.sh` — NEW `check_http_listener()` after `check_functional()` (line 497): sources `/etc/nora/nora-mcp.env`; if `NORA_MCP_TRANSPORT=stdio` or unset → record `http.listener skip` + return 0 silently; otherwise `bash -c "exec 3<>/dev/tcp/127.0.0.1/${PORT}"` TCP-probe + optional `curl --max-time 3 -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}${PATH}"`. Use `command -v curl` gate.
- [x] 7.3 Wire `check_http_listener` into `main()` after `check_functional` only when `[ "${CHECK_HTTP}" = "1" ]`.
- [x] 7.4 Verify: `bash -n scripts/verify-install.sh` exits 0; manual `sudo scripts/verify-install.sh --check-http` on an HTTP-enabled host exits 0.

Commit: `feat(verifier): check_http_listener opt-in probe for non-stdio transport`

---

## Task 8: `httpx` explicit dev pin

Removes transitive-flake risk (httpx currently rides along via FastMCP).

- [x] 8.1 `pyproject.toml` — append `"httpx>=0.27"` to `[dependency-groups].dev` list (alphabetical-ish, after `hypothesis`).
- [x] 8.2 Verify: `uv lock && uv sync --dev` exits 0; `uv run python -c 'import httpx; print(httpx.__version__)'` exits 0.

Commit: `chore(deps): pin httpx in [dependency-groups].dev`

---

## Task 9: Integration test run (full suite gate)

- [x] 9.1 `uv run python -m pytest --cov=src/nora --cov-report=term-missing` exits 0; coverage ≥85%; `test_http_transport_smoke` happy-path green.
- [x] 9.2 `uv run ruff check .` exits 0; `uv run ruff format --check .` exits 0; `uv run mypy --strict src/nora` exits 0.
- [x] 9.3 `bash -n scripts/install.sh && bash -n scripts/bootstrap.sh && bash -n scripts/verify-install.sh` all exit 0.
- [x] 9.4 Manual smoke (local): `uv run nora-mcp --transport=http --port=8765 &` then `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/mcp` returns a non-zero HTTP status (proves bind); kill child.

No separate commit — this is the verify gate for the merge.

---

## Task 10: ADR + INSTALL/OPERATIONS docs

- [x] 10.1 NEW `docs/adr/0001-nora-mcp-namespace.md` (no `docs/adr/` exists yet — create dir) — short ADR (≤40 lines) on `NORA_MCP_*` namespace choice: precedent (`NORA_OID_*`, `NORA_INTERVENTIONS_*`, `NORA_HITL_*`), rejected `FASTMCP_*` passthrough (couples operator contract to library), scope-limited to `cli.py` (not `Settings`).
- [x] 10.2 `INSTALL.md` lines 273–275 — replace "NORA is stdio-only" with: "NORA defaults to stdio. To enable HTTP/SSE: edit `/etc/nora/nora-mcp.env` (`NORA_MCP_TRANSPORT=http`, `_HOST=127.0.0.1`, `_PORT=8005`), then `sudo systemctl restart nora-mcp` (no `daemon-reload` needed for env-only changes)."
- [x] 10.3 `OPERATIONS.md` after line 125 — add "Transport modes" subsection: table of boot log lines per transport (`stdio` / `http` / `sse`); note that `verify-install.sh --check-http` is the opt-in HTTP probe.
- [x] 10.4 Verify: ADR file exists; `grep -q 'NORA_MCP_TRANSPORT' INSTALL.md` exits 0; `grep -q '/etc/nora/nora-mcp.env' OPERATIONS.md` exits 0.

Commit: `docs(transport): ADR + INSTALL/OPERATIONS updates for NORA_MCP_* namespace`

---

## Task 11: PR creation (subject to user approval)

- [ ] 11.1 Open PR against `main` with title `feat(cli): configurable transport — http/sse via NORA_MCP_* env + --transport flag (closes #34)`. PR description links the spec/design/tasks artifacts under `openspec/changes/2026-09-13-http-sse-transport/`. **Closes #34** in PR body.
- [ ] 11.2 PR body checklist mirrors design § "Success Criteria" (7 bullets from `proposal.md` lines 55–61).

No commit — PR is the merge boundary.

---

## Dependency Graph

```
1 (RED scaffold)  ──► 2 (GREEN cli.py)  ──► 9 (integration gate)  ──► 11 (PR)
                                  └──► 3 (.env template) ──┐
3 (.env template) ──► 4 (install.sh phase_env_file_mcp)  ──┤
4                ──► 5 (bootstrap.sh forwarding)          ├──► 9
4                ──► 6 (systemd EnvironmentFile=)         ┤
6 + 3            ──► 7 (verify-install.sh --check-http)  ┤
                    ──► 8 (httpx dev pin)  ───────────────┘
                                       9 ──► 10 (ADR + INSTALL/OPERATIONS) ──► 11
```

Tasks 3-8 can land in any order after Task 2; Task 9 gates Task 10; Task 10 gates Task 11.

## Risks

- **T1 RED pass-by-accident** — if any of the 6 RED tests passes against current stdio `cli.py`, the assertion is wrong. Re-verify each fails.
- **T2 catalog-guard ordering** — `verify_tools_are_catalogued(catalog_registry)` at line 66 MUST run before transport resolution; moving it would silently allow rogue tool registration.
- **T4 signing-key leak** — `phase_env_file_mcp` MUST NOT touch `NORA_OID_CATALOG_SIGNING_KEY`; that line stays in `phase_env_file`.
- **T7 default-on HTTP probe** — `--check-http` MUST remain opt-in (default off) so stdio-only operators see no regression.
