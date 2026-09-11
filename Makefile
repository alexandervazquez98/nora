# NORA — convenience targets. Always invoke the venv interpreter directly so
# `make` works without `uv run` in CI environments.

VENV     := .venv
PY       := $(VENV)/bin/python
PIP      := $(VENV)/bin/pip
UV       := uv

.PHONY: help install sync lock test lint type format format-fix run clean

help:
	@echo "NORA Makefile targets:"
	@echo "  make install  - create the .venv and install pinned deps via uv"
	@echo "  make sync     - alias for install"
	@echo "  make lock     - re-resolve the uv.lock file from pyproject.toml"
	@echo "  make test     - run pytest via the venv interpreter"
	@echo "  make lint     - ruff check (linter)"
	@echo "  make format   - ruff format --check (no writes)"
	@echo "  make format-fix - ruff format (writes)"
	@echo "  make type     - mypy --strict src/nora"
	@echo "  make run      - boot the MCP server over stdio"
	@echo "  make clean    - remove .venv, build artefacts, caches"

install: sync

sync:
	$(UV) sync

lock:
	$(UV) lock

test:
	$(PY) -m pytest

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
