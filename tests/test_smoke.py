"""Smoke tests for the NORA package toolchain bootstrap.

These tests fail before the package manifest (`pyproject.toml`), the
`src/nora/` package, and the test runner configuration exist. They MUST
pass after task 1.2 lands.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_package_is_importable() -> None:
    """`import nora` succeeds and exposes a non-empty `__version__`."""
    import nora

    assert isinstance(nora.__version__, str)
    assert nora.__version__, "nora.__version__ must be a non-empty string"


def test_pytest_discovers_smoke_test_without_extra_config() -> None:
    """`python3 -m pytest --collect-only -q` from the project root finds this file."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, (
        f"pytest --collect-only failed with exit code {result.returncode}.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "test_smoke.py" in result.stdout, (
        f"pytest --collect-only did not list test_smoke.py.\nstdout:\n{result.stdout}"
    )
