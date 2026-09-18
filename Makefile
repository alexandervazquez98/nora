# NORA — convenience targets. Always invoke the venv interpreter directly so
# `make` works without `uv run` in CI environments.

VENV     := .venv
PY       := $(VENV)/bin/python
PIP      := $(VENV)/bin/pip
UV       := uv

.PHONY: help install sync lock test test-fast test-one watch lint type format format-fix run clean

help:
	@echo "NORA Makefile targets:"
	@echo "  make install    - create the .venv and install pinned deps via uv"
	@echo "  make sync       - alias for install"
	@echo "  make lock       - re-resolve the uv.lock file from pyproject.toml"
	@echo "  make test       - full pytest suite WITH coverage (CI / pre-commit)"
	@echo "  make test-fast  - parallel pytest WITHOUT coverage (dev loop)"
	@echo "  make test-one   - run a single test by name pattern: make test-one K=foo"
	@echo "  make watch      - auto-rerun tests on file changes (dev loop)"
	@echo "  make lint       - ruff check (linter)"
	@echo "  make format     - ruff format --check (no writes)"
	@echo "  make format-fix - ruff format (writes)"
	@echo "  make type       - mypy --strict src/nora"
	@echo "  make run        - boot the MCP server over stdio"
	@echo "  make clean      - remove .venv, build artefacts, caches"

install: sync

sync:
	$(UV) sync

lock:
	$(UV) lock

test:
	$(PY) -m pytest --cov=src/nora --cov-report=term-missing

# Fast iteration target: parallel (pytest-xdist), no coverage, no cache.
# Mirrors the `addopts` defaults but explicit so a developer sees the
# intent. Override parallelism with `PYTEST_XDIST_WORKERS=N`.
test-fast:
	$(PY) -m pytest -n auto --no-cov

# Run a single test by name pattern. Usage:
#   make test-one K=snmp_get
#   make test-one K=hitl_tokens::test_signing
test-one:
	@if [ -z "$(K)" ]; then \
		echo "Usage: make test-one K=<name-substring>" >&2; \
		exit 2; \
	fi
	$(PY) -m pytest -n auto --no-cov -x -k "$(K)"

# Auto-rerun tests on file changes. Excludes `.venv`, `.git`, caches.
# Honors the same parallel/no-coverage defaults as `test-fast`.
watch:
	$(PY) -m pytest_watch -- -n auto --no-cov --ignore=.venv --ignore=.git \
		--ignore=.pytest_cache --ignore=.ruff_cache --ignore=.mypy_cache

lint:
	$(PY) -m ruff check .

format:
	$(PY) -m ruff format --check .

format-fix:
	$(PY) -m ruff format .

type:
	$(PY) -m mypy --strict src/nora

run:
	$(PY) -m nora

clean:
	rm -rf $(VENV) .pytest_cache .ruff_cache .mypy_cache .coverage
	find . -type d -name '__pycache__' -prune -exec rm -rf {} +
	find . -type d -name '*.egg-info' -prune -exec rm -rf {} +
