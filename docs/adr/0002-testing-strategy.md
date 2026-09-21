# ADR-0002: Test suite performance and isolation strategy

## Status

Accepted — 2026-09-20. Codifies the test feedback-loop optimizations
that landed incrementally via issue #46 and the follow-up work in
`feat/test-perf-stdio-fixture`.

## Context

The NORA test suite grew to **764 tests across 67 files** (~26 k LOC).
Cold start of `python -m nora mcp` is **~1.3 s**. Many tests boot a
fresh `nora-mcp` subprocess per assertion (`subprocess.run([python, "-m",
"nora"], ...)`), paying 1.3 s × N tests in serial mode. The suite
reached **105 s+** in serial mode, creating friction for TDD loops
(5-6 minutes of waiting per session, red-green-refactor).

This ADR codifies the strategy that emerged from issue #46
(`test(perf): paralelizar suite con pytest-xdist`, closed) and the
follow-up stdio-fixture work in `feat/test-perf-stdio-fixture`.

## Decisions

### 1. `pytest-xdist` parallelism is opt-in, not default

`pytest -n auto --no-cov -m "not no_xdist"` runs the dev loop in parallel.
It is intentionally NOT in the default `addopts` because xdist worker
spawn adds overhead per pytest invocation, hurting single-file runs
(`make test-one K=foo`) by ~5×. Dev-loop parallelism is opt-in via
`make test-fast`, `make test-one`, and `make watch`.

### 2. Session-scoped subprocess fixtures per worker

Two fixtures share the `nora-mcp` boot cost across tests in the same
worker:

- `mcp_http_server` (session-scoped, transport=http)
- `mcp_stdio_server` (session-scoped, transport=stdio)

Both use `scope="session"` plus the `worker_id` parameter so each
pytest-xdist worker boots exactly one process on a hermetic tmp tree.
Tests in the same worker pay only the JSON-RPC round-trip cost per
assertion, not the full ~1.3 s cold start.

The trade-off: tests that need per-boot observation (stderr framing,
exit code, env-var injection, per-test temp dirs for tool output) cannot
use these fixtures and must remain inline. The canonical list lives in
the docstring of each fixture in `tests/conftest.py` and in
`odd/tasks/test-perf-stdio-fixture.md`.

### 3. Do NOT capture `os.environ` in fixtures

Fixtures must build a fixed env dict of NORA-specific vars only:

```python
env = {
    "NORA_OID_CATALOG_SIGNING_KEY": BUILTIN_BASELINE_SIGNING_KEY,
    "NORA_OID_CATALOGS_PATH": str(tmp / "catalogs"),
    "NORA_DEVICES_INVENTORY_PATH": str(tmp / "devices.yaml"),
    # ...no **os.environ
}
```

Capturing `os.environ` (`env = {**os.environ, ...}`) creates cross-test
contamination under xdist: tests that mutate env vars before the
fixture boots (e.g. `test_subprocess_silently_ignores_legacy_llm_env_keys`,
`test_subprocess_nora_mcp_exits_nonzero_on_empty_signing_key`) break later
tests in the same worker. Both `mcp_stdio_server` and `mcp_http_server`
currently have this bug — tracked as a separate follow-up. New fixtures
must not inherit the pattern.

### 4. Mark xdist-incompatible tests with `@pytest.mark.no_xdist`

Tests that race under xdist (filesystem state on `/tmp/nora-bootstrap-*`,
TCP port binding on a fixed port, signal timing) are excluded from
`make test-fast` (parallel dev loop). They still run in `make test`
(sequential CI).

The marker is not a generic ignore mechanism. Do NOT use it to silence
real flakes — fix the underlying test or fixture.

### 5. CI runs sequentially, not parallel

`.github/workflows/ci.yml` runs `make test` (sequential with coverage).
Stability over speed for the canonical CI gate. The pre-PR local run
matches CI exactly: `make test` locally first, then push.

## Consequences

- **Dev loop parallel**: ~25 s (was 105 s+; **~4× faster**)
- **Dev loop serial**: ~104 s (was 105 s+; **~1% faster**, basically
  free since the shared fixture doesn't help in serial)
- **Migrated tests**: 2 transport-agnostic tests in
  `tests/test_integration.py` (`test_subprocess_handles_malformed_json_gracefully`,
  `test_boot_with_register_device_round_trip`). The remaining tests in
  that family were analyzed in `odd/tasks/test-perf-stdio-fixture.md`
  WU-5 / WU-6 and intentionally remain inline.
- **Known flake** under full-suite order (~10% of runs):
  `test_boot_with_register_device_round_trip`. Cause: both fixtures
  capture `os.environ` (decision #3 violated). Out of scope for the
  follow-up that landed in `feat/test-perf-stdio-fixture`; tracked as
  a separate PR.

## References

- Issue #46: `test(perf): paralelizar suite con pytest-xdist` (closed)
- [`odd/tasks/test-perf-speedup.md`](odd/tasks/test-perf-speedup.md) — Original xdist work
- [`odd/tasks/test-perf-stdio-fixture.md`](odd/tasks/test-perf-stdio-fixture.md) — Stdio fixture + WU-5 / WU-6 analysis + flake note
- Commits on `feat/test-perf-stdio-fixture` (this branch):
  - `0d43a46` — `McpStdioClient` (WU-2)
  - `630a3f9` — `mcp_stdio_server` fixture (WU-3)
  - `5db62a3` — migrate `test_integration.py` (WU-4)
  - `6e53db9`, `5d48f25`, `5d00804` — docs / close follow-up #1 (WU-6/7)
