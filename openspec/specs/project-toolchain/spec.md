# project-toolchain Specification

## Purpose

Defines the build, lint, format, test, and type-check toolchain every NORA Phase 1 contributor MUST use. It guarantees byte-identical reproducibility via `uv` + PEP 621 + committed lockfile, enforces strict TDD on `pytest`+`pytest-cov`, gates merge on `ruff` and `mypy --strict`, and locks the `src/nora/` + `tests/` layout every later phase inherits.

## Requirements

### Requirement: Reproducible Python Environment

The project MUST pin the interpreter to `3.12` via `.python-version`, declare all dependencies in PEP 621 `pyproject.toml`, and commit `uv.lock` so `uv sync` produces a byte-identical virtual environment on every machine.

#### Scenario: clean bootstrap reproduces the pinned interpreter

- GIVEN a clean checkout with `.python-version`, `pyproject.toml`, and `uv.lock`
- WHEN `uv sync` runs on a fresh machine
- THEN a `.venv/` is created
- AND the interpreter inside reports Python 3.12

#### Scenario: lockfile drift is rejected

- GIVEN `pyproject.toml` is edited to add a dependency without re-resolving
- WHEN `uv sync --frozen` runs
- THEN the command exits non-zero and names the drifted dependency

### Requirement: Strict TDD Test Runner

Tests MUST run via `python3 -m pytest`, MUST include `pytest-cov`, and the coverage threshold MUST start at 0 until a later change raises it.

#### Scenario: suite collects and reports coverage

- GIVEN `tests/` mirrors `src/nora/`
- WHEN `python3 -m pytest` runs
- THEN every collected test executes
- AND a coverage table for `src/nora/` is printed

#### Scenario: failing test exits non-zero

- GIVEN a test asserts `1 == 2`
- WHEN the suite runs
- THEN pytest names the failing test and exits non-zero

#### Scenario: syntax error in a test is reported with location

- GIVEN `tests/test_x.py` contains a syntax error
- WHEN the suite runs
- THEN pytest prints the file path, line, and offending token
- AND exits non-zero

### Requirement: Ruff Lint and Format Gates

`ruff check .` and `ruff format --check .` MUST exit 0 on every tracked file. The configuration MUST ban `print(...)` inside `src/nora/` to keep stdout clean for the MCP JSON-RPC stream.

#### Scenario: clean tree lints green

- GIVEN `src/nora/` and `tests/` contain no violations
- WHEN `ruff check .` runs
- THEN the command exits 0 with no diagnostics

#### Scenario: banned `print()` is flagged

- GIVEN `src/nora/server.py` contains `print("debug")`
- WHEN `ruff check .` runs
- THEN the line is reported with a rule code and the exit code is non-zero

#### Scenario: formatting drift is detected

- GIVEN a file in `src/nora/` is under-formatted
- WHEN `ruff format --check .` runs
- THEN the file path is listed and the exit code is non-zero

### Requirement: Strict Type Checking

`mypy --strict src/nora` MUST exit 0; every public function MUST carry parameter and return annotations; implicit `Any` MUST be rejected.

#### Scenario: fully annotated module passes

- GIVEN a module with full annotations and no `Any`
- WHEN `mypy --strict src/nora` runs
- THEN the command exits 0

#### Scenario: missing annotation is flagged

- GIVEN a public function without a return annotation
- WHEN `mypy --strict src/nora` runs
- THEN the function name and line are reported
- AND the exit code is non-zero

### Requirement: Source and Test Layout

Source code MUST live under `src/nora/`, tests MUST live under `tests/` mirroring that tree, and `.gitignore` MUST exclude `.venv/`, `__pycache__/`, `.pytest_cache/`, `.coverage`, and `*.egg-info/`.

#### Scenario: pytest discovers mirrored tests without extra config

- GIVEN `src/nora/foo/bar.py` and `tests/foo/test_bar.py` exist
- WHEN `python3 -m pytest` runs
- THEN `tests/foo/test_bar.py` is collected with no path arguments

#### Scenario: build artefacts never enter the index

- GIVEN `uv sync` and a test run have completed
- WHEN `git status --ignored` runs
- THEN `.venv/`, `__pycache__/`, `.pytest_cache/`, and `.coverage` are listed as ignored

### Requirement: Discoverable Make Targets

A `Makefile` MUST expose `make test`, `make lint`, `make type`, `make format`, and `make run` that wrap the configured commands.

#### Scenario: `make test` wraps the configured runner

- GIVEN the `Makefile` is present
- WHEN `make test` runs
- THEN it invokes `python3 -m pytest`
- AND no other shell side effects occur

### Requirement: Security Boundary — No Credentials in Toolchain Artefacts

`pyproject.toml`, `uv.lock`, `Makefile`, and `.python-version` MUST NOT contain real credentials, package URLs with embedded tokens, or any value from `.env`.

#### Scenario: `.env` values are never read by the toolchain

- GIVEN `.env` exists with `GEMINI_API_KEY`
- WHEN `uv sync`, `ruff`, `mypy`, or `pytest` runs
- THEN the value is not read or echoed by any of them

#### Scenario: lockfile pins exact versions

- GIVEN `uv.lock` is committed
- WHEN a reviewer inspects any dependency entry
- THEN the entry contains an exact version and hash, not a floating range

### Requirement: Observability — Toolchain Output Stays on stderr

Ruff, mypy, and pytest diagnostics MUST go to stderr and MUST NOT pollute stdout; `nora-mcp-server` relies on this contract.

#### Scenario: failing test prints to stderr, not stdout

- GIVEN a test fails
- WHEN `python3 -m pytest 2>/dev/null` runs (stderr suppressed)
- THEN no failure text is visible
- WHEN `python3 -m pytest 2>&1 >/dev/null` runs (stdout suppressed)
- THEN the failure text is visible
