# Archive Report: nora-mcp-thin-split

> Archive-time terminal record. Authoritative for the state of the change at close.
> Intermediate snapshots (`apply-progress.md`, `verify-report.md`) describe the
> work at the time they were written; work continued after them and is reflected
> here. Intermediate claims of "pending" / "blocked" / "open gap" are not
> echoed as current facts.

## Change

- **Name**: `nora-mcp-thin-split`
- **Archive folder**: `openspec/changes/archive/2026-09-12-nora-mcp-thin-split/`
- **Archived at**: 2026-09-12
- **Mode**: openspec (filesystem canonical specs + engram archive-report observation)
- **Cycle**: 4th archive attempt. Attempts 1–3 each blocked at compose for
  malformed deltas; attempt 3 additionally moved the change folder prematurely
  before all composes passed. Attempt 4 (this one) completed cleanly.

## Final State

### Specs synced (canonical → post-compose byte counts)

| Domain | Pre | Post | Δ | Compose exit | Diff -r readback |
|---|---|---|---|---|---|
| nora-mcp-server | 11565 | 9650 | −1915 | 0 | empty |
| secure-configuration | 5478 | 3743 | −1735 | 0 | empty |
| llm-provider-interface | 5759 | 382 | −5377 | 0 | empty |
| session-journal | 23003 | 2243 | −20760 | 0 | empty |
| **Total** | **45805** | **16018** | **−29787** | — | — |

All four `gentle-ai sdd-archive-compose` invocations exited 0; their atomic
`.compose-tmp → canonical` mv's each completed; the mandatory `diff -r`
between snapshot and destination was empty for every domain and for the
archive move itself.

### Compose command invocations and exits

```
gentle-ai sdd-archive-compose --canonical openspec/specs/nora-mcp-server/spec.md         --delta openspec/changes/nora-mcp-thin-split/specs/nora-mcp-server/spec.md         --output openspec/specs/nora-mcp-server/spec.md.compose-tmp
EXIT_CODE: 0

gentle-ai sdd-archive-compose --canonical openspec/specs/secure-configuration/spec.md     --delta openspec/changes/nora-mcp-thin-split/specs/secure-configuration/spec.md     --output openspec/specs/secure-configuration/spec.md.compose-tmp
EXIT_CODE: 0

gentle-ai sdd-archive-compose --canonical openspec/specs/llm-provider-interface/spec.md  --delta openspec/changes/nora-mcp-thin-split/specs/llm-provider-interface/spec.md  --output openspec/specs/llm-provider-interface/spec.md.compose-tmp
EXIT_CODE: 0

gentle-ai sdd-archive-compose --canonical openspec/specs/session-journal/spec.md          --delta openspec/changes/nora-mcp-thin-split/specs/session-journal/spec.md          --output openspec/specs/session-journal/spec.md.compose-tmp
EXIT_CODE: 0
```

### Mandatory readback (archive folder vs pre-move snapshot)

```
diff -r "$SNAPSHOT_ROOT/source" "openspec/changes/archive/2026-09-12-nora-mcp-thin-split"
DIFF_EXIT: 0
```

Empty diff. Snapshot was `cp -R openspec/changes/nora-mcp-thin-split
/tmp/sdd-archive.XXXXXX/source` taken immediately before `git mv`. The
`archive-report.md` file was written **after** this readback so it is additive
only and is not part of the comparison.

### Archive contents (10 files, 116,690 bytes)

```
openspec/changes/archive/2026-09-12-nora-mcp-thin-split/apply-progress.md        12,823
openspec/changes/archive/2026-09-12-nora-mcp-thin-split/design.md                11,352
openspec/changes/archive/2026-09-12-nora-mcp-thin-split/explore.md               29,542
openspec/changes/archive/2026-09-12-nora-mcp-thin-split/proposal.md               3,400
openspec/changes/archive/2026-09-12-nora-mcp-thin-split/specs/llm-provider-interface/spec.md   1,459
openspec/changes/archive/2026-09-12-nora-mcp-thin-split/specs/nora-mcp-server/spec.md           9,240
openspec/changes/archive/2026-09-12-nora-mcp-thin-split/specs/secure-configuration/spec.md      5,772
openspec/changes/archive/2026-09-12-nora-mcp-thin-split/specs/session-journal/spec.md          2,983
openspec/changes/archive/2026-09-12-nora-mcp-thin-split/tasks.md                  8,911
openspec/changes/archive/2026-09-12-nora-mcp-thin-split/verify-report.md         34,210
```

Note: the `specs/*/spec.md` sizes above are the **archived delta specs** (the
change's own `## ADDED/REMOVED/RENAMED/MODIFIED` blocks), not the merged
canonical. The merged canonical lives under `openspec/specs/{domain}/spec.md`.

### Task Completion Gate

`openspec/changes/nora-mcp-thin-split/tasks.md` had 37 tasks across Groups
0–7. All 37 boxes were `[x]` before archive began. Gate passed; no
reconciliation performed.

### Source of Truth updated

- `openspec/specs/nora-mcp-server/spec.md` — final post-compose: 11 requirements
  (down from 12: REMOVED `nora_health Tool Contract`, REMOVED `Edge Cases`).
  RENAMED `R-NEW-1 — Three Read-Only Tools on the Global MCP Instance` →
  `R-NEW-1 — Four @mcp.tool Registrations`. The four surviving tools surface
  now owns the secret-redaction contract (formerly in secure-configuration).
- `openspec/specs/secure-configuration/spec.md` — final post-compose: 4
  requirements (down from 8). Two requirements
  (`Credentials Never Appear in String Representations` and
  `Settings Load Status Is Observable`) appear in **both** the delta's
  `## REMOVED Requirements` block (with a "re-issued under MODIFIED"
  Reason note) **and** the `## MODIFIED Requirements` block. See the
  "Semantic concern" section below.
- `openspec/specs/llm-provider-interface/spec.md` — final post-compose: 0
  requirements (down from 9). Purpose paragraph retained; all 9 requirements
  REMOVED in a single delta.
- `openspec/specs/session-journal/spec.md` — final post-compose: 0 requirements
  (down from 21). Purpose paragraph and `## Cross-References` section
  retained; all 21 R1–R21 requirements REMOVED in a single delta.

### Verification status

`openspec/changes/archive/2026-09-12-nora-mcp-thin-split/verify-report.md`
(34,210 bytes) reflects the cycle close. No CRITICAL issues remained at
verification time. Two post-verify gaps were closed by `Group 7` (4 tasks):

1. CRITICAL — `secure-configuration` Scenario "accidental staging is
   rejected" had no implementation (no `.pre-commit-config.yaml`, only
   `.git/hooks/pre-commit.sample`). Closed by:
   - `tests/test_precommit_guard.py` (17 tests across 3 layers: regex pinning,
     script contract, `.pre-commit-config.yaml` wiring)
   - `scripts/check-no-env-staged.sh` (executable bash guard)
   - `.pre-commit-config.yaml` (`repos: [local]`, `stages: [pre-commit]`)
2. WARNING — `secure-configuration` Scenario "missing required setting fails
   fast" was only covered at the `Settings()` layer, not end-to-end.
   Closed by:
   - `tests/test_integration_boot.py::test_subprocess_nora_mcp_exits_nonzero_on_empty_signing_key`
   - `tests/test_integration_boot.py::test_subprocess_python_dash_m_nora_exits_nonzero_on_empty_signing_key`

Both entry points (`nora-mcp` and the deprecation-aliased `python -m nora`)
now boot-against-empty-signing-key pin the abort contract.

Final verification numbers per `verify-report.md` summary line: 278 pytest,
85.18% coverage, mypy --strict clean, ruff check + format clean,
pre-commit guard script tested.

### SDD cycle complete

The change has been fully proposed, designed, implemented, verified, and
archived. The four canonical specs now reflect the thin MCP server surface
(one driver + three intervention memory tools, no LLM, no journal). Ready
for the next change.

## Semantic concern (recorded for traceability, not blocking)

The `secure-configuration` delta declares two requirements in **both** the
`## REMOVED Requirements` block and the `## MODIFIED Requirements` block:

- `Credentials Never Appear in String Representations` — REMOVED with
  Reason note "Examples used `GEMINI_API_KEY`; re-issued under MODIFIED
  with `nora_oid_catalog_signing_key`." MODIFIED with updated scenario
  bodies that reference `nora_oid_catalog_signing_key`.
- `Settings Load Status Is Observable` — REMOVED with Reason note
  "Examples used `NORA_LLM_PROVIDER` and `nora_health`; re-issued under
  MODIFIED." MODIFIED with updated scenario bodies.

The `gentle-ai sdd-archive-compose` command applies RENAMED → MODIFIED →
REMOVED → ADDED in that order, so the final canonical state is REMOVED for
both. The post-compose canonical no longer carries the
`Credentials Never Appear in String Representations` contract (it now lives
only in `nora-mcp-server/spec.md`'s MODIFIED `Security Boundary — No
Secrets in Tool Responses`, which covers the tool-response side but not
`repr(settings)` or log lines), and no longer carries the
`Settings Load Status Is Observable` contract (the `loaded_from` field is
not asserted anywhere).

This is the mechanical result of the composer's deterministic ordering; the
delta's authoring intent (per the Reason notes) was apparently to MODIFY,
not REMOVE. The compose command exited 0 and the skill rule is
"non-zero exit = blocking failure, zero exit = continue", so this archive
proceeded. The concern is recorded here so that a future reader who notices
the missing contracts knows the mechanism and can decide whether to
re-issue them in a follow-up change.

## Files of interest

- `openspec/changes/archive/2026-09-12-nora-mcp-thin-split/proposal.md` — change intent.
- `openspec/changes/archive/2026-09-12-nora-mcp-thin-split/design.md` — architecture and Group ordering.
- `openspec/changes/archive/2026-09-12-nora-mcp-thin-split/tasks.md` — 37 checked tasks across Groups 0–7.
- `openspec/changes/archive/2026-09-12-nora-mcp-thin-split/verify-report.md` — verification evidence.
- `openspec/changes/archive/2026-09-12-nora-mcp-thin-split/apply-progress.md` — apply snapshot (ranked below this report per Final-State Authority).
- `openspec/changes/archive/2026-09-12-nora-mcp-thin-split/specs/{nora-mcp-server,secure-configuration,llm-provider-interface,session-journal}/spec.md` — archived delta specs (the change's own delta blocks, not the merged canonical).
- `openspec/specs/{nora-mcp-server,secure-configuration,llm-provider-interface,session-journal}/spec.md` — merged canonical (post-compose).