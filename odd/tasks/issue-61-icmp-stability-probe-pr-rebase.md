# Feature: ICMP Stability Probe — PR rebase & chain merge (issue #61)

## Goal

Re-attempt the merge of the three chained PRs (#63, #64, #65) that close
issue #61 (sector continuous ping probe, RFC 3550 metrics, PDF export,
OpenChat delivery) into the current `main`. The implementation already
existed on local+remote branches (`feat-icmp-pr1-engine`,
`feat-icmp-pr2-metrics`, `feat-icmp-pr3-pdf`) but they diverged from `main`
at `fefd77d` (v0.3.0→v0.3.1) while `main` is now at `4c6399a` (v0.3.5).
The work had never reached a green test run against the current mainline.

Closes #61 once the three PRs merge successfully in chain order.

## Context

- Branch base: `main` @ `4c6399a` (clean working tree, no contamination
  from #45).
- Worktree path: `../enlaces-icmp-rebase`.
- Branch name: `feat/issue-61-pr-rebase`.
- 7 commits of `feat-icmp-pr3-pdf` not yet on `main`; ~10 merges in `main`
  since the divergence point. Estimated rebase complexity: medium
  (tooling-surface overlap with #72 prompts and #70 spectrum changes).
- The auto-triage comment on issue #61 already defined the PR titles,
  bases, and merge order; we honor them at PR-open time.

## Design Decisions (frozen)

- **D1 — Single integration branch.** We rebased
  `feat-icmp-pr3-pdf` (which already contains PR1+PR2+PR3 in commit
  order) on top of current `main`, instead of rebase-then-merge three
  times. The chained-PR skill is honored at PR-open time
  (#63 base=main → #64 base=#63 → #65 base=#64), but locally we worked
  against the integrated branch so we resolved conflicts once.
- **D2 — Conflict resolution policy.** Where main and the ICMP work
  both touched the same area, both sides' additions were preserved
  (additive changes — main added spectrum and ICMP added probe; no
  semantic conflict). Where counts changed (e.g. `_EXPOSED_PROMPTS`
  allow-list, `Settings.model_fields` field count, expected tool
  lists in tests), the count was updated to the post-merge reality and
  every manual decision documented in the rebase commit message body.
- **D3 — Validation gate.** Before declaring rebase complete: `pytest`
  green, `ruff check` clean, `mypy --strict` clean on
  `src/nora/probes/**`, the full probe surface boots in `nora-mcp`
  (smoke `prompts/list` + `tools/list`).
- **D4 — PR chain.** PR1 (#63) opens from `feat-icmp-pr1-engine`
  rebased onto main; PR2 (#64) from `feat-icmp-pr2-metrics` rebased
  onto PR1; PR3 (#65) from `feat-icmp-pr3-pdf` rebased onto PR2. Only
  the user's decision to push & open PRs triggers the remote side.

## Out of Scope

- New probe features not in the issue #61 body.
- Issue #45 work (prompt versioning); stays in its own worktree/branch.
- Issue #60 (TUI installer); blocked by #45.
- LLM eval harness half of issue #72 (already excluded by user on
  2026-09-20).
- Production deployment; this only validates the chain can land in main.

## Tasks

Each task closes with at least one work-unit commit on
`feat/issue-61-pr-rebase`, with tests alongside behavior, using
Conventional Commit messages.

### T1 — Worktree bootstrap & rebase preflight — DONE

- Worktree created at `../enlaces-icmp-rebase` on branch
  `feat/issue-61-pr-rebase` from `main` @ `4c6399a`.
- Baseline: `pytest` 755 passed, 3 skipped on pre-rebase main.
- Risk surface identified: 12 files touched by both sides since
  `fefd77d`; 2 high-risk (`src/nora/server.py`, `tests/test_server.py`).
- Evidence: T1 entry + `git status` clean + baseline pytest run.

### T2 — Rebase `feat-icmp-pr3-pdf` onto current `main` — DONE

- `git rebase --onto main fefd77d feat-icmp-pr3-pdf`.
- Resolved 14 conflict points across 8 files (see resolution log
  below).
- After rebase, the integration branch contains 7 new commits on top of
  current main with the full PR1+PR2+PR3 chain.

### T3 — Validation gate — DONE

- `pytest` full suite: **868 passed, 4 skipped** (baseline was 755
  passed, 3 skipped; +113 ICMP tests now passing).
- `ruff check src/nora tests`: **All checks passed**.
- `mypy --strict src/nora/probes/`: **Success: no issues found in 15
  source files**.
- Smoke: `mcp.list_prompts()` returns **19 entries** (16 tool-specs +
  `netops_orchestrator` + `snmp_pmp450i`); `mcp.list_tools()` returns
  **19 entries** matching the README Tier-0/1/2 tables.

### T4 — Commit-to-PR-slice mapping — DONE

See **Commit map** section below.

### T5 — Prepare PR descriptions & status report — DONE

For each of #63, #64, #65, copy the description skeleton from the
issue's automated triage comment and adapt (do NOT open PRs without
user authorization). Present a status report with: rebase outcome,
validation results, PR chain map, three PR descriptions ready to
copy-paste into `gh pr create`.

### PR description drafts

#### PR #63 — ICMP unprivileged engine + coordinator + MCP server wiring (PR1)

```markdown
# PR #63 — feat(probes): ICMP sector stability probe — PR1 (engine + coordinator + MCP wiring)

Closes #61 (PR1 of 3).

## Scope

Implements the first of three chained PRs that together deliver the
ICMP sector stability probe (issue #61):

- **Unprivileged ICMP engine** (`src/nora/probes/icmp.py`) — async
  datagram socket receiver, no `CAP_NET_RAW` requirement. Defensive
  bind to a non-privileged port, anti-spoof sequence numbers, and
  strict timeout enforcement.
- **Settings extension** — 6 new fields under `Settings.nora_icmp_*`
  (`default_duration_seconds`, `default_interval_seconds`,
  `default_packet_size_bytes`, `per_packet_timeout_seconds`,
  `max_duration_seconds`, `min_duration_seconds`), enforced by a new
  `_validate_icmp_duration_bounds` boot-time validator.
- **SM discovery** (`src/nora/probes/discovery.py`) — async helper
  that queries `snmp_get_sm_table` and produces typed `ProbeTarget`
  objects (`src/nora/probes/models.py`) for the AP and every
  online SM (filterable via `target_luids`).
- **Async coordinator** (`src/nora/probes/probe.py`) — `run_probe()`
  paces 1 packet/sec to each destination in parallel via
  `asyncio.wait_for`, with graceful shutdown on cancellation.
- **MCP wiring** (`src/nora/server.py`) — three new `@mcp.tool`
  registrations: `icmp_run_sector_stability_probe`,
  `icmp_get_sector_stability_progress`,
  `icmp_cancel_sector_stability_probe`. Each companion tool-spec is
  authored under `docs/tool_specs/`. `Settings.nora_probe_results_dir`
  is documented for the next PR.

## Test surface

- `tests/probes/test_icmp_engine.py` — 334 LOC, async engine unit
  tests (RTT distribution, loss detection, timeout, sequence
  integrity).
- `tests/probes/test_discovery.py` — 410 LOC, SM discovery +
  ProbeTarget validation.
- `tests/probes/test_probe_coordinator.py` — 732 LOC, coordinator
  pacing, cancellation, parallel-target fan-out.

## Validation

- `pytest`: 868 passed, 4 skipped (rebase landing); baseline 755 + 113
  new ICMP tests.
- `ruff check src/nora tests`: All checks passed.
- `mypy --strict src/nora/probes/`: Success: no issues found in 15
  source files.

## Dependency on next PRs

This PR lands the engine, coordinator, and tool wiring. **PR #64**
adds RFC 3550 metrics + 8-state diagnostic + atomic JSON persistence.
**PR #65** adds PDF report + Markdown inline + OpenChat delivery.
The three PRs must merge in order: #63 → #64 → #65.
```

#### PR #64 — RFC 3550 metrics + 8-state diagnostic + atomic JSON persistence (PR2)

```markdown
# PR #64 — feat(probes): RFC 3550 metrics + 8-state diagnostic + atomic JSON persistence

Closes #61 (PR2 of 3). Base: #63.

## Scope

Builds on the engine + coordinator from #63 and adds the **metrics
layer**:

- **RFC 3550 jitter** (`src/nora/probes/metrics.py`) — standard
  exponential moving average:
  `Jitter_i = Jitter_{i-1} + (|D(i-1,i)| - Jitter_{i-1}) / 16`
- **8-state diagnostic matrix** (`src/nora/probes/diagnostic.py`) —
  per-node verdict (EXCELLENT, JITTER_MODERATE, JITTER_CRITICAL,
  LOSSY_LINK, UNSTABLE_DROPS, SECTOR_BACKHAUL_BOTTLENECK,
  SECTOR_RF_CONGESTION, ISOLATED_SUBSCRIBER_FAULT) plus the
  differential Δ metrics (ΔRTT = RTT_SM − RTT_AP and
  ΔJitter = Jitter_SM − Jitter_AP) that isolate the RF airlink from
  the wired backhaul.
- **Atomic JSON persistence** (`src/nora/probes/persistence.py`) —
  `tempfile + os.replace` to write `<nora_probe_results_dir>/PRB-<sector>-<ts>-<hex>.json`
  so a partial write can never leave a corrupt record on disk.
- **Settings extension** — `nora_probe_results_dir` defaults to
  `./var/probes/`.

## Test surface

- `tests/probes/test_metrics.py` — 266 LOC, RFC 3550 formula
  regression vs the issue body examples.
- `tests/probes/test_diagnostic.py` — 8-state classifier matrix
  coverage + ΔRTT / ΔJitter math.
- `tests/probes/test_persistence.py` — 321 LOC, atomic-write
  guarantees under simulated partial-write failures.
- `tests/probes/test_sector_delta.py` — 164 LOC, sector differential
  isolation logic.

## Validation

Same gate as PR1 — `pytest` 868 / `ruff` clean / `mypy --strict` clean
on `src/nora/probes/`. No new dependencies.

## Dependency on next PR

**PR #65** adds PDF report + Markdown inline + OpenChat delivery.
This PR's atomic JSON is the input to PR3's PDF generator.
```

#### PR #65 — PDF report + Markdown inline + OpenChat delivery + unprivileged ICMP sysctl (PR3)

```markdown
# PR #65 — feat(probes): PDF report + Markdown inline + OpenChat delivery + unprivileged ICMP sysctl

Closes #61 (PR3 of 3). Base: #64.

## Scope

Closes out the user-facing surface for the ICMP sector stability
probe (issue #61):

- **PDF report** (`src/nora/probes/pdf.py`, dep: `reportlab>=4.0`) —
  executive header (sector, frequency, band, ISO timestamp,
  duration), per-node telemetry table (Tx/Rx, %loss, RTT min/avg/
  median/p95/max, RFC 3550 jitter, ΔRTT vs AP, individual
  diagnostic), time-series of loss bursts, recommendations.
- **Markdown inline** (`src/nora/probes/markdown.py`) — concise
  operator-facing summary rendered in the chat alongside the JSON.
- **OpenChat delivery** (`src/nora/probes/operator_identification.py`)
  — operator alias resolution + file attachment via the Open WebUI
  `/api/v1/files/` endpoint; falls back to a static download link
  under `<nora-host>:<port>/reports/stability_<sector>_<ts>.pdf` when
  the API is unreachable.
- **Unprivileged ICMP sysctl** — `INSTALL.md` documents the
  `sysctl -w net.ipv4.ping_group_range=0 1000` (or equivalent)
  requirement so the probe works without `CAP_NET_RAW`. `scripts/install.sh`
  applies it idempotently.
- **`icmp_list_probe_runs` tool** — companion `@mcp.tool` that lists
  recent `PRB-*.json` records from disk, with sanitizer applied.

## Test surface

- `tests/probes/test_pdf.py` — 304 LOC, PDF generation smoke + byte
  invariants.
- `tests/probes/test_markdown.py` — 185 LOC, Markdown inline shape.
- `tests/probes/test_operator_identification.py` — 92 LOC, alias
  resolution.
- `tests/probes/test_sanitize_probe.py` — 94 LOC, IP/credential
  scrubbing in the persisted JSON.
- Existing `tests/probes/test_*` for engine / coordinator / metrics
  remain green.

## Validation

- `pytest`: 868 passed, 4 skipped.
- `ruff check src/nora tests`: All checks passed.
- `mypy --strict src/nora/probes/`: Success.
- `mcp.list_prompts()` returns 19 entries; `mcp.list_tools()` returns
  19 entries. README Tier-0 table now lists all 4 ICMP tools.

## Dependency

None — this is the chain's terminal PR. After merge, issue #61 is
fully delivered.

## Note for the reviewer

The rebase fix commit (`7b5911d`) lands with this PR — it
re-authorises the tool spec for `icmp_list_probe_runs` (PR1 wiring
registered the `@mcp.tool` without its companion spec), restores the
3 `@mcp.prompt` wrappers that auto-merge dropped during PR1, and
updates `test_config.py` field-count assertion to 31 (final
post-reconcile value).
```

### T6 — Final definition of done — PENDING

- [x] Rebase complete on `feat/issue-61-pr-rebase`.
- [x] pytest + ruff + mypy clean.
- [x] `prompts/list` and `tools/list` show 19 entries each.
- [x] Commit-by-PR mapping documented.
- [x] PR descriptions drafted (not opened).
- [ ] User authorizes push & PR open.

## Sequencing

T1 → T2 → T3 → T4 → T5 → T6

T3 is the gate. If rebase fails validation, T2 is reopened, never
skipped.

## Branch & Commits

- Branch: `feat/issue-61-pr-rebase`.
- All commits stay on the local branch; no push, no PR until user decides.

### Commit map (8 commits on top of `main` @ `4c6399a`)

| PR slice | Commit | Subject |
|----------|--------|---------|
| (scaffold, no PR) | `4d3cd76` | docs(odd): scaffold issue #61 ICMP stability probe plan |
| **#63 PR1** (base=main) | `bb7cb5a` | feat(probes): ICMP unprivileged engine + Settings (issue #61 WU-1.1+1.4) |
| **#63 PR1** | `17044a8` | feat(probes): SM-discovery helper + typed ProbeTarget models (issue #61 WU-1.2) |
| **#63 PR1** | `a142c6c` | feat(probes): async coordinator run_probe() with paced ICMP per destination (issue #61 WU-1.3) |
| **#63 PR1** | `826f77a` | feat(probes): MCP server wiring + tool specs + in-process registry (issue #61 WU-1.5+1.6) |
| **#64 PR2** (base=#63) | `9888d30` | feat(probes): RFC 3550 metrics + 8-state diagnostic + atomic JSON persistence (issue #61 PR2) |
| **#65 PR3** (base=#64) | `6fce5c0` | feat(probes): PDF report + Markdown inline + OpenChat delivery + unprivileged ICMP sysctl (issue #61 PR3) |
| (rebase-only, lands in PR3) | `7b5911d` | fix(rebase): reconcile ICMP PR1+PR2+PR3 rebase onto current main |

The scaffold commit is documentation only — lives on
`feat/issue-61-pr-rebase` but not in any of the three PRs. The fix
commit is the rebase reconciliation (new tool spec for
`icmp_list_probe_runs`, restored 3 `@mcp.prompt` wrappers, extended
`_EXPOSED_PROMPTS`, orchestrator Tier-0 list update, `test_config`
field-count update to 31, ruff line-length fix) — it logically belongs
in PR3 since PR3 was the commit that exposed `icmp_list_probe_runs`
without its companion spec.

### Conflict-by-conflict resolution log

The rebase produced 14 conflict points across 8 files. All resolved
mechanically except where noted:

| File | Conflicts | Strategy |
|------|-----------|----------|
| `.env.example` | 1 (PR1) | Keep both — spectrum sweep (main) + ICMP probe (PR1) sections. |
| `src/nora/config.py` | 1 auto-merged (PR1) | Auto-merge; both sides added disjoint fields. |
| `tests/test_config.py` | 4 (PR1 ×2, PR2 ×2) | Field-count assertion updated incrementally: 28 (after PR1) → 29 (after PR2) → 31 (final). Docstring narratives updated to match. |
| `tests/test_integration.py` | 1 (PR1) | `expected` set extended from 15 → 18 tool names. Docstring updated. |
| `tests/test_integration_boot.py` | 4 (PR1 ×2, PR3 ×2) | Lists extended: 15 → 18 (PR1), then 18 → 19 (PR3 with `icmp_list_probe_runs`). Ruff E501 trimmed on line 241. |
| `tests/test_main_alias.py` | 1 (PR1) | `expected` set extended from 15 → 18 tool names. |
| `tests/test_server.py` | 2 (PR1 ×2) | `expected` sets extended from 15 → 18 tool names (×2 distinct tests). |
| `docs/tool_specs/README.md` | 2 (PR1, PR3) | Tier-0 table merged: HEAD had 10 entries (with `icmp_list_probe_runs` and `nora_get_tool_spec`), PR1 added the 3 ICMP tools, PR3 redundantly re-added `icmp_list_probe_runs`. Final: 13 Tier-0 entries. |

`src/nora/server.py` had no conflict markers — auto-merged cleanly
because the three PRs all add new `@mcp.tool` / `@mcp.prompt` blocks in
distinct locations. The fix commit (`7b5911d`) restored the 3
`@mcp.prompt` wrappers and `_EXPOSED_PROMPTS` entries that auto-merge
dropped during PR1.

## Related (not in this feature)

- Issue #45 — prompt versioning; lives on its own branch/worktree,
  untouched here.
- Issue #60 — TUI installer; blocked by #45.
- Issues #47, #56 — separate RFCs awaiting maintainer decision.