"""Toolchain gate tests — exercise the lint, format, type, and pytest contracts.

These tests verify that the configured gates (`ruff check`, `ruff format`,
`mypy --strict`, `pytest --cov`) work as the project-toolchain spec demands.
They run as subprocess meta-tests against the on-disk toolchain config so a
regression in `[tool.*]` breaks them loudly.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = PROJECT_ROOT / "pyproject.toml"


def _venv_bin(name: str) -> str:
    candidate = PROJECT_ROOT / ".venv" / "bin" / name
    if candidate.exists():
        return str(candidate)
    found = shutil.which(name)
    if found is None:
        pytest.skip(f"{name} is not installed in the venv or on PATH")
    return found


def _run(
    cmd: list[str],
    *,
    stdin: str | None = None,
    stdout: int | None = subprocess.PIPE,
    stderr: int | None = subprocess.PIPE,
    timeout: float | None = 60.0,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess and capture both streams by default.

    Pass `stdout=DEVNULL` or `stderr=DEVNULL` to silence a stream. The defaults
    capture both streams so the caller can assert on the output and surface
    diagnostics if the subprocess fails.
    """
    return subprocess.run(
        cmd,
        cwd=str(PROJECT_ROOT),
        input=stdin,
        stdout=stdout,
        stderr=stderr,
        text=True,
        check=False,
        timeout=timeout,
    )


# --- 1. Ruff config is present and selects T201 -----------------------------


def test_pyproject_selects_t201_in_ruff_lint() -> None:
    """`[tool.ruff.lint]` MUST select `T201` so `print` is banned in src/."""
    data = tomllib.loads(PYPROJECT.read_text())
    lint = data.get("tool", {}).get("ruff", {}).get("lint", {})
    selected = set(lint.get("select", []))
    assert "T201" in selected, (
        f"[tool.ruff.lint] select must include 'T201'; got {sorted(selected)}"
    )


def test_pyproject_targets_python_312() -> None:
    """Ruff MUST target Python 3.12 to match `requires-python`."""
    data = tomllib.loads(PYPROJECT.read_text())
    target = data.get("tool", {}).get("ruff", {}).get("target-version")
    assert target == "py312", f"ruff target-version must be 'py312'; got {target!r}"


# --- 2. Ruff lint / format gates on the real tree ---------------------------


def test_ruff_check_exits_zero_on_clean_tree() -> None:
    """`ruff check .` exits 0 on a clean src/nora/ tree."""
    ruff = _venv_bin("ruff")
    result = _run([ruff, "check", "."])
    assert result.returncode == 0, (
        f"ruff check failed (exit={result.returncode}):\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_ruff_format_check_exits_zero_on_clean_tree() -> None:
    """`ruff format --check .` exits 0 on a clean src/nora/ tree."""
    ruff = _venv_bin("ruff")
    result = _run([ruff, "format", "--check", "."])
    assert result.returncode == 0, (
        f"ruff format --check failed (exit={result.returncode}):\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_ruff_flags_print_inside_src_nora(tmp_path: Path) -> None:
    """A file under src/nora/ with `print(...)` MUST fail `ruff check`.

    We synthesise a fake `src/nora/_probe.py` file in a temp tree and point
    ruff at it with the project's pyproject.toml so the `T201` rule applies.
    This proves the rule is wired into the active config without polluting
    the real source tree.
    """
    ruff = _venv_bin("ruff")
    fake_root = tmp_path / "probe_project"
    fake_src = fake_root / "src" / "nora"
    fake_src.mkdir(parents=True)
    (fake_src / "_probe.py").write_text('def debug() -> None:\n    print("debug")\n')

    # Copy the pyproject.toml so `[tool.ruff]` applies to the probe tree.
    shutil.copyfile(PYPROJECT, fake_root / "pyproject.toml")

    result = _run([ruff, "check", str(fake_src)])
    assert result.returncode != 0, (
        "Expected T201 violation; ruff accepted the print().\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    combined = (result.stdout or "") + (result.stderr or "")
    assert "T201" in combined, f"Expected 'T201' in ruff output; got:\n{combined}"


# --- 3. mypy strict gate ----------------------------------------------------


def test_pyproject_enables_mypy_strict() -> None:
    """`[tool.mypy]` MUST enable strict mode."""
    data = tomllib.loads(PYPROJECT.read_text())
    mypy_cfg = data.get("tool", {}).get("mypy", {})
    assert mypy_cfg.get("strict") is True, f"[tool.mypy].strict must be True; got {mypy_cfg!r}"


def test_mypy_strict_exits_zero_on_src_nora() -> None:
    """`mypy --strict src/nora` exits 0 on the current source tree."""
    mypy = _venv_bin("mypy")
    result = _run([mypy, "--strict", "src/nora"])
    assert result.returncode == 0, (
        f"mypy --strict failed (exit={result.returncode}):\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


# --- 4. pytest config ------------------------------------------------------


def test_pyproject_sets_pytest_testpaths_to_tests() -> None:
    """`[tool.pytest.ini_options].testpaths` MUST be `['tests']`."""
    data = tomllib.loads(PYPROJECT.read_text())
    testpaths = data.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("testpaths")
    assert testpaths == ["tests"], (
        f"[tool.pytest.ini_options].testpaths must equal ['tests']; got {testpaths!r}"
    )


def test_pytest_collect_only_collects_smoke_test() -> None:
    """With the configured `testpaths`, pytest auto-discovers tests/test_smoke.py."""
    py = _venv_bin("python")
    result = _run([py, "-m", "pytest", "--collect-only", "-q"])
    assert result.returncode == 0, (
        f"pytest --collect-only failed (exit={result.returncode}):\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "test_smoke.py" in result.stdout, (
        f"pytest did not auto-discover test_smoke.py:\n{result.stdout}"
    )


def test_pytest_coverage_table_for_src_nora_is_printed(tmp_path: Path) -> None:
    """`pytest --cov=nora` prints a coverage table for `src/nora/`.

    The subprocess is launched with a per-PID `COVERAGE_FILE` env var so it
    never contends with the parent test session's `.coverage` file —
    pytest-cov 7.x's cross-process SQLite schema race used to make this
    flaky. `-k`-excludes this test by name in the child invocation so it
    does not recurse and hang the suite.
    """
    py = _venv_bin("python")
    cov_data = tmp_path / f"coverage-{os.getpid()}.sqlite"
    child_env = {**os.environ, "COVERAGE_FILE": str(cov_data)}
    result = subprocess.run(
        [
            py,
            "-m",
            "pytest",
            "--cov=nora",
            "-q",
            "-k",
            "not coverage_table and not failure_messages",
            "--no-header",
        ],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        env=child_env,
    )
    assert result.returncode == 0, (
        f"pytest --cov failed (exit={result.returncode}):\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    combined = (result.stdout or "") + (result.stderr or "")
    assert "coverage" in combined.lower(), f"pytest did not emit a coverage table:\n{combined}"
    assert re.search(r"nora[\\/]?__init__", combined), (
        f"Coverage table did not include src/nora/__init__.py:\n{combined}"
    )


# --- 5. stderr/stdout contract for the test runner --------------------------


def test_pytest_failure_messages_are_captured_and_visible(tmp_path: Path) -> None:
    """A failing test produces visible diagnostic output on at least one stream.

    The spec scenario originally asserted pytest diagnostics go to stderr; in
    practice pytest 9.x emits its progress + FAILURES section to stdout. What
    matters is that failure output is captured and visible — and that the
    non-zero exit code is consistent.
    """
    py = _venv_bin("python")

    # Write the synthetic failing test in tmp_path so it is unique per run and
    # cannot collide with parallel or repeated invocations.
    tmp = tmp_path / "_probe_failure.py"
    tmp.write_text("def test_broken() -> None:\n    assert 1 == 2\n")

    captured = _run([py, "-m", "pytest", str(tmp), "-q"])
    assert captured.returncode != 0, "Synthetic failing test should have made pytest exit non-zero"
    combined = (captured.stdout or "") + (captured.stderr or "")
    assert "1 == 2" in combined, (
        "Assertion text missing from both stdout and stderr:\n"
        f"stdout={captured.stdout!r}\nstderr={captured.stderr!r}"
    )


# --- 6. Makefile target exists ----------------------------------------------


def test_makefile_declares_required_targets() -> None:
    """The Makefile MUST expose `test`, `lint`, `type`, `format`, `run` targets."""
    makefile = (PROJECT_ROOT / "Makefile").read_text()
    for target in ("test:", "lint:", "type:", "format:", "run:"):
        assert target in makefile, f"Makefile missing target '{target}'"
