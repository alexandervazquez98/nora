# Feature: Governance docs (CONTRIBUTING + ADR-0002 + OPERATIONS update)

**Branch:** `feat/test-perf-stdio-fixture` (continuing the same worktree / branch)
**Status:** authorized — user chose "Solo docs" option (Recommended) for governance to make future developers follow correct test-suite usage

## Goal

Document the test-suite performance strategy and dev workflow so future contributors (human or AI) follow the patterns that emerged from issue #46 and the follow-up work in this branch:

1. Use shared fixtures (`mcp_http_server`, `mcp_stdio_server`) instead of inline subprocess boots when transport-agnostic.
2. Don't capture `os.environ` in fixtures (causes flake under xdist).
3. Mark flaky-under-xdist tests with `@pytest.mark.no_xdist`.
4. Use the right Makefile target for the workflow (`test-fast` for dev, `test` for CI/pre-PR, `test-one K=` for single, `watch` for autoreload).
5. Pre-commit hook can fail (`gga` provider config issue) — `--no-verify` documented as escape hatch for docs-only commits.
6. AI-agent discipline: verify branch before each commit, use worktree if running alongside another session, use `--timeout=30` on pytest to avoid hangs.

## Non-goals

- Pre-commit hooks (lint/format/type enforcement): out of scope per user choice
- CI gate additions (PR comments, auto-flake detection): out of scope
- `AGENTS.md` for AI-specific guidance: out of scope (covered by CONTRIBUTING.md generically)
- Any change to `src/nora/*` or test code

## Acceptance criteria

- [ ] `CONTRIBUTING.md` created at repo root with:
  - Quick start (make install/test/test-fast/test-one/watch/lint/format/type)
  - "Test execution workflow" section (dev loop / pre-PR / CI)
  - "Adding tests" with "use existing fixtures when possible" + list of stdio-specific cases that must remain inline
  - "Adding fixtures" with the `scope="session"` + `worker_id` recipe and the **do-not-capture-os.environ** warning
  - "Pre-commit hooks" section (current single hook + gga escape hatch)
  - "Branch discipline" section (verify branch, worktree for AI sessions)
  - Cross-references to ADR-0002 and odd/tasks/test-perf-stdio-fixture.md
- [ ] `docs/adr/0002-testing-strategy.md` created with:
  - Status: Accepted, 2026-09-20
  - Context (the suite growth + cold-start cost)
  - 5 decisions: xdist parallelism opt-in, session-scoped subprocess fixtures, no `os.environ` capture, `no_xdist` marker, CI sequential
  - Consequences (metrics: 105s → 25s parallel, 105s → 104s serial)
  - References (issue #46, work-unit docs, commits)
- [ ] `OPERATIONS.md` updated to add a prominent "For developers" pointer section near the top that links to CONTRIBUTING.md and ADR-0002 (the existing "Test execution workflow" section stays but is now cross-linked)
- [ ] Work-unit commit with Conventional Commits message
- [ ] No new dependencies, no source code changes

## Work-units

### WU-A — Task file scaffold (this doc)

Already done as part of this feature bootstrap.

### WU-B — CONTRIBUTING.md + ADR-0002 + OPERATIONS.md pointer (single commit)

Write all 3 in one WU because they form a coherent set of governance docs.

## References

- Issue #46: `test(perf): paralelizar suite con pytest-xdist`
- odd/tasks/test-perf-speedup.md: original xdist work
- odd/tasks/test-perf-stdio-fixture.md: stdio fixture + lessons learned (parent work-unit for this governance work)
- commits 0d43a46, 630a3f9, 5db62a3, 6e53db9, 5d48f25, 5d00804 (the WU-2..7 commits on this branch)