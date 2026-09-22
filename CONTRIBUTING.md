# Contributing to NORA

Welcome. This guide covers the day-to-day workflow for adding code, tests,
and documentation to the NORA codebase. For operator-facing guidance (install,
deploy, key management, OpenChat integration), see [`OPERATIONS.md`](OPERATIONS.md).
For the architecture decision log, see [`docs/adr/`](docs/adr/).

## Quick start

```bash
make install    # uv sync into .venv
make test-fast  # parallel pytest without coverage (~25s)
make test       # full sequential with coverage (~104s, CI / pre-PR)
```

For everything else, see the [Makefile](Makefile) (`make help` lists all targets)
or the [test execution workflow](#test-execution-workflow) below.

## Test execution workflow

The repo has three modes for running tests, picked automatically by what
you're doing.

### Dev loop (fast iteration)

For TDD red-green-refactor cycles where you only care about a few tests
at a time:

| Command | Use when |
| --- | --- |
| `make test-fast` | You want feedback on a non-trivial change (~25 s, xdist parallel, no coverage) |
| `make test-one K=<pattern>` | You want to focus on one test (xdist parallel, `-x` to stop on first failure) |
| `make watch` | You're iterating on a single file and want auto-rerun on save (`pytest-watch`) |

`make test-fast` skips tests marked `@pytest.mark.no_xdist` (those that race
under xdist due to filesystem state or TCP port binding — see
[Marking slow / flaky tests](#marking-slow--flky-tests)). It still runs
everything else across multiple workers.

### Pre-PR (full suite with coverage)

Before opening a PR, run the full sequential suite with coverage. This is
the canonical "does my change break anything" gate:

```bash
make test       # sequential pytest with --cov; ~104 s
```

This is what CI runs. If it fails locally, your PR will fail in CI.

### CI (GitHub Actions)

CI is defined in [`.github/workflows/ci.yml`](.github/workflows/ci.yml).
It runs **sequentially** (not parallel) for stability — the same `make test`
target as pre-PR. PRs get a green check when all jobs pass.

## Adding tests

The biggest performance lever in this repo is whether a test boots a
fresh subprocess or reuses a shared fixture. The default should always
be the latter.

### Use existing fixtures when possible

The repo has two session-scoped subprocess fixtures, one per transport:

| Fixture | Transport | Use for |
| --- | --- | --- |
| `mcp_http_server` | HTTP (streamable-http on `/mcp`) | Tests that don't care about framing |
| `mcp_stdio_server` | stdio (newline-delimited JSON on stdin/stdout) | Tests that don't care about framing |

Both are session-scoped **per pytest-xdist worker**: with `scope="session"`
plus the `worker_id` parameter, each worker boots exactly one
`nora-mcp` process on a free OS-allocated port (HTTP) or hermetic
stdin/stdout pipes (stdio), on a hermetic tmp tree (per-worker catalogs
+ devices.yaml). Tests in the same worker pay only the JSON-RPC
round-trip cost per assertion (~10 ms), not the ~1.3 s cold start.

To use the stdio fixture, request it as a test parameter and pass it to
`McpStdioClient`:

```python
def test_my_thing(mcp_stdio_server):
    client = McpStdioClient(mcp_stdio_server)
    response = client.initialize()
    ...
```

To use the HTTP fixture, do the same with `mcp_http_client`:

```python
def test_my_thing(mcp_http_client):
    response = mcp_http_client.tools_list()
    ...
```

### When to write inline subprocess (legacy pattern)

Some tests legitimately need a fresh subprocess per test because they
assert on per-boot behaviour. The fixture's docstring in
[`tests/conftest.py`](tests/conftest.py) lists the canonical examples.
The summary:

- **Per-boot stderr framing** (e.g. `test_subprocess_emits_structured_startup_log_on_stderr`)
- **Per-boot exit code** (e.g. `test_subprocess_nora_mcp_exits_nonzero_on_empty_signing_key`)
- **Per-boot env-var injection** (e.g. `test_subprocess_silently_ignores_legacy_llm_env_keys`)
- **Per-test temp dir for tool output** (e.g. `test_save_intervention_record_lands_via_stdio` in
  `tests/intervention_writer/test_stdio_smoke.py`)

If you're adding a new test and none of these apply, **use the shared
 fixture**. Boot cost adds up: a test suite with 40 inline-subprocess
tests pays 40 × 1.3 s = 52 s of boot alone. The same suite using the
shared fixture pays ~13 s of boot (one per worker) + the round-trips.

For the migration rationale and the list of tests that intentionally
remain inline, see
[`odd/tasks/test-perf-stdio-fixture.md`](odd/tasks/test-perf-stdio-fixture.md).

### Marking slow / flaky tests

Two markers are defined in [`pyproject.toml`](pyproject.toml):

```python
@pytest.mark.slow
def test_my_long_running_thing(): ...


@pytest.mark.no_xdist
def test_my_flaky_under_xdist(): ...
```

- `@pytest.mark.slow` — tests that take >1 s for a non-subprocess reason
  (e.g. real hardware simulation). Run them in CI, skip them in tight
  loops with `pytest -m "not slow"`.
- `@pytest.mark.no_xdist` — tests that race under pytest-xdist because
  they share filesystem state (`/tmp/nora-bootstrap-*`) or TCP ports
  between workers. `make test-fast` skips them via
  `-m "not no_xdist"`; `make test` (CI, sequential) still runs them.

**Do not** mark tests `no_xdist` to silence real flakes. The pattern
exists to isolate known xdist-incompatible tests while we work on
  resolving them — not as a generic ignore mechanism.

#### Adding fixtures

The recipe for any fixture that boots an external resource (subprocess,
 server, etc.):

```python
@pytest.fixture(scope="session")
def my_resource(worker_id: str, tmp_path_factory: pytest.TempPathFactory):
    # 1. Allocate per-worker state (tmp dir, free port, ...)
    tmp = tmp_path_factory.mktemp(f"my_resource_{worker_id}")
    ...

    # 2. Build a fixed env dict (NOT *os.environ — see below)
    env = {
        "NORA_SPECIFIC_VAR":": "value",
        ...
    }

    # 3. Spawn the resource
    proc = subprocess.Popen([...], env=env, ...)

    # 4. Wait for readiness (with bounded timeout, NOT sleep)
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail(...)
        try:
            # probe
            break
        except Exception:
            time.sleep(0.1)
    else:
        proc.kill(); proc.wait()
        pytest.fail("resource never became ready")

    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        # drain pipes
```

Three rules:

1. **Use `scope="session"` plus `worker_id` for per-worker isolation.**
   Without `worker_id`, parallel workers will collide on shared
   resources (ports, filesystem paths).
2. **Build a fixed env dict, do NOT capture `os.environ`.** This is the
   single most common cause of cross-test contamination under xdist.
   `mcp_stdio_server` and `mcp_http_server` both have this bug
   (documented in `odd/tasks/test-perf-stdio-fixture.md` "Known flake").
   A separate PR will fix both; new fixtures must NOT inherit the
   pattern.
3. **Probe readiness instead of sleeping.** `time.sleep(N)` is a flake
   waiting to happen. Poll with a bounded deadline.

## Pre-commit hooks

The repo has one pre-commit hook installed (see
[`.pre-commit-config.yaml`](.pre-commit-config.yaml)):

- `no-env-staging` — rejects `git add .env` (per the secure-config spec).

It does NOT run `ruff` / `mypy` / pytest by default. Run them manually
before pushing via `make lint && make format && make type && make test`.

`gga` (Gentleman Guardian Angel) is configured for AI code review at
PR time (`gga run --pr-mode`). It may fail to parse the provider
config in some environments; if it blocks a docs-only commit, use
`git commit --no-verify` and document why in the commit body. Code
commits should not bypass it.

## Branch discipline

If you're an AI agent or human working alongside another session on
the same repo (e.g. multiple Pi sessions in different terminals, or
worktrees-and), the filesystem is shared — `git checkout` from one
session affects the others.

Verify before each commit:

```bash
git branch --show-current  # MUST be your branch
git status --short         # MUST show only your files
```

If your branch changed unexpectedly (another session checked it out
under you), recover with `git update-ref`:

```bash
git update-ref refs/heads/feat/YOUR-BRANCH <your-commit-oid>
git checkout feat/YOUR-BRANCH
```

Or use a worktree for full isolation:

```bash
git worktree add /tmp/my-isolated-work feat/YOUR-BRANCH
cd /tmp/my-isolated-work
```

Always use `--timeout=30` on pytest invocations during TDD work —
subprocess-based tests can hang if the server crashes without closing
stdin/stdout, and pytest-timeout gives you a clean failure.

## References

- [ADR-0001](docs/adr/0001-nora-mcp-namespace.md) — `NORA_MCP_*` namespace
- [ADR-0002](docs/adr/0002-testing-strategy.md) — Test suite performance and isolation strategy
- [`odd/tasks/test-perf-speedup.md`](odd/tasks/test-perf-speedup.md) — Original xdist work
- [`odd/tasks/test-perf-stdio-fixture.md`](odd/tasks/test-perf-stdio-fixture.md) — Stdio fixture + lessons learned
- [`OPERATIONS.md`](OPERATIONS.md) — Operator-facing runbook (install, deploy, keys)
