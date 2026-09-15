# Tasks: 3-Tier Tool Service-Impact Governance (issue #43)

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | ~1055 (code+tests ~675, docs ~380) |
| 400-line budget risk | High |
| Chained PRs recommended | Yes |
| Suggested split | PR #1 → PR #2 → PR #3 → PR #4 |
| Delivery strategy | ask-on-risk |
| Chain strategy | pending |

Decision needed before apply: Yes
Chained PRs recommended: Yes
Chain strategy: stacked-to-main|feature-branch-chain|size-exception|pending
400-line budget risk: High

| # | Goal | PR | Test | Harness | Rollback |
|---|------|----|------|---------|----------|
| 1 | HMAC + Settings | 1 | `pytest test_hitl_tokens test_config` | `nora hitl mint` | `tokens.py` + `config.py` |
| 2 | CLI dispatcher | 2 | `pytest test_cli test_main_alias test_cli_hitl` | `python -m nora` + `nora hitl mint` | `__main__.py` + new `cli_hitl.py` |
| 3 | Tier-1 gate | 2 | `pytest test_snmp_spectrum` | `mcp` stdio spectrum | `spectrum.py` + `exceptions.py` |
| 4 | Validator + 12 docs | 3 | `pytest test_prompts` | `ls docs/tool_specs/` | new `docs/tool_specs/` + validator |
| 5 | Multi-dir + §6 | 3 | `pytest test_prompts` | `nora-mcp` boot | `registry.py` + `netops_orchestrator.md` |
| 6 | Tier wiring + integration | 4 | `pytest --cov=src/nora` | `python -m nora` stdio | `server.py` + tests |
| 7 | Docs + release notes | 4 | `pytest` (regression) | N/A | OPERATIONS + INSTALL + RELEASE-NOTES |

## PR #1: Foundations

- [ ] 1.1 RED: HMAC scenarios in `test_hitl_tokens.py` (payload, `compare_digest`, tampered, stub, kill-switch).
- [ ] 1.2 GREEN: `tokens.py` — `signature` field; HMAC-SHA256 `mint_token`; `verify_approval_token` recomputes + `compare_digest`. Literal `_REJECTED_MESSAGE` verbatim.
- [ ] 1.3 RED: Settings scenarios in `test_config.py` for `nora_hitl_signing_key` + `nora_tool_specs_dir`.
- [ ] 1.4 GREEN: `config.py` + `.env.example` — both fields/placeholders.

## PR #2: Operator Surface

- [ ] 2.1 RED: `test_cli_hitl.py` (happy/missing/kill-switch).
- [ ] 2.2 GREEN: `__main__.py` → argparse dispatcher; create `cli_hitl.py`. No `nora-hitl` console script.
- [ ] 2.3 RED: `test_snmp_spectrum.py` — `operator_confirmed=False` raises, `True` proceeds, outside-window refuses.
- [ ] 2.4 GREEN: add `Tier1ClearanceRequired(DriverError)`; gate in `spectrum.py` BEFORE any SNMP GET; `operator_confirmed=False` param in `server.py`.

## PR #3: Governance Content

- [ ] 3.1 RED: tool-spec scenarios in `test_prompts.py` (`tier: 0|1|2`, ADR-4 invariants, `PromptNotFoundError` on invalid tier).
- [ ] 3.2 GREEN: `registry.py` — `ToolSpecValidator` enforcing ADR-4 schema; README exempt from `tier`.
- [ ] 3.3 Author 12 files in `docs/tool_specs/` (11 specs + README) per frozen schema.
- [ ] 3.4 RED: multi-dir scenarios in `test_prompts.py` (both dirs scanned, missing dir non-fatal, body names `Tier 0/1/2` + `operator_confirmed=True` + HITL token).
- [ ] 3.5 GREEN: `PromptRegistry.scan` → sequence of dirs; `from_settings` reads tool-specs dir; `netops_orchestrator.md` adds §6 with `<!-- tool_spec: name -->` markers.

## PR #4: Integration + Documentation

- [ ] 4.1 RED: integration scenarios in `test_integration_boot.py` + `test_server.py` for 12-tool tier classification.
- [ ] 4.2 GREEN: verify `server.py` boot keeps `PromptRegistry.from_settings(settings)` wired (R-NEW-9); run `pytest --cov=src/nora`, confirm ≥85%.
- [ ] 4.3 RED: `.env.example` content scans in `test_config.py`.
- [ ] 4.4 Author `OPERATIONS.md`, `INSTALL.md`, `RELEASE-NOTES.md` (hard-break legacy tokens, `operator_confirmed`, kill switch).