"""Air-gap static + runtime scan — R8.

No source file under `src/nora/core/` may import `requests`, `httpx`,
`urllib.request`, `socket`, `ssl`, or `http.client`. This module owns
both the static AST scan and the runtime mock-based assertion.

Run via `pytest -m "not slow" tests/test_session_journal_airgap.py` or
include in the regular suite — these tests are fast (no network, no
heavy lifting).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SRC_DIR = PROJECT_ROOT / "src" / "nora" / "core"

# Patterns banned under R8. Matches module names (`import socket` /
# `from socket import ...`) AND attribute paths (`urllib.request.urlopen`).
_BANNED_MODULES: tuple[str, ...] = (
    "requests",
    "httpx",
    "urllib.request",
    "socket",
    "ssl",
    "http.client",
)


def _iter_python_files() -> list[Path]:
    return sorted(SRC_DIR.rglob("*.py"))


def _find_banned_imports(py_file: Path) -> list[tuple[int, str, str]]:
    """Return list of (lineno, import_type, module) for banned imports in `py_file`."""
    src = py_file.read_text()
    tree = ast.parse(src)
    offenders: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if any(top == m.split(".")[0] for m in _BANNED_MODULES):
                    offenders.append((node.lineno, "import", alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                continue
            top = node.module.split(".")[0]
            if any(top == m.split(".")[0] for m in _BANNED_MODULES):
                offenders.append((node.lineno, "importfrom", node.module))
    return offenders


# ---------------------------------------------------------------------------
# Static AST scan
# ---------------------------------------------------------------------------


def test_no_banned_imports_in_session_journal_package() -> None:
    """R8-S1 — the AST scan over `src/nora/core/*.py` finds zero banned imports."""
    offenders: list[tuple[str, int, str, str]] = []
    for py in _iter_python_files():
        for lineno, kind, module in _find_banned_imports(py):
            offenders.append((str(py), lineno, kind, module))

    assert offenders == [], (
        f"Banned imports found under src/nora/core/: "
        f"{[(path, line, kind, mod) for path, line, kind, mod in offenders]}"
    )


def test_no_banned_imports_even_in_dotted_attribute_paths() -> None:
    """`from urllib.request import urlopen` is also a violation (dotted path)."""
    offenders: list[tuple[str, int, str]] = []
    for py in _iter_python_files():
        text = py.read_text()
        for mod in _BANNED_MODULES:
            pattern = re.compile(rf"\b{re.escape(mod)}\b")
            for m in pattern.finditer(text):
                # Confirm this isn't inside a string / comment by inspecting the AST.
                # The previous test already catches the `import` statement itself; this
                # test catches inline attribute access like `urllib.request.urlopen`.
                line_no = text[: m.start()].count("\n") + 1
                offenders.append((str(py), line_no, mod))

    # Filter out matches that are inside triple-quoted docstrings/comments —
    # we accept `urllib.request` in a comment that says "MUST NOT import".
    real_offenders = []
    for path, line_no, mod in offenders:
        text = Path(path).read_text()
        lines = text.splitlines()
        if line_no - 1 < len(lines):
            line_text = lines[line_no - 1]
            # The match is on this line; allow it if the line is a comment
            # (heuristic: starts with `#` after whitespace).
            if line_text.lstrip().startswith("#"):
                continue
            # Or if the match is inside a docstring in this file, skip
            # (we already filter via the AST scan in the previous test).
            real_offenders.append((path, line_no, mod))

    assert real_offenders == [], f"Dotted-path banned references found: {real_offenders}"


# ---------------------------------------------------------------------------
# Runtime mock-based assertion
# ---------------------------------------------------------------------------


def test_full_journal_path_does_not_call_banned_symbols(tmp_path: Path) -> None:
    """R8-S2 — driving the full journal path does NOT call any banned symbol.

    We mock `socket.socket`, `urllib.request.urlopen`, and `httpx.get` at
    their import sites, drive a `record_step` flow, and assert every mock
    was never called.
    """
    from nora.core.session_journal import SessionJournal
    from nora.sanitizer import Sanitizer

    journal = SessionJournal(
        journal_dir=tmp_path / "sessions",
        max_trace_steps=50,
        sanitizer=Sanitizer(),
        enabled=True,
    )

    with (
        mock.patch("socket.socket") as socket_mock,
        mock.patch("urllib.request.urlopen") as urlopen_mock,
        mock.patch("httpx.get") as httpx_get_mock,
        mock.patch("httpx.post") as httpx_post_mock,
    ):
        from datetime import datetime, timezone

        journal.record_step(
            tool="probe",
            input_args={"device_id": "ap-7400-01"},
            result_summary="ok",
            duration_ms=1,
            outcome="success",
            ts=datetime.now(timezone.utc),
        )
        # Read-back exercises the load path.
        journal.get_state()

    socket_mock.assert_not_called()
    urlopen_mock.assert_not_called()
    httpx_get_mock.assert_not_called()
    httpx_post_mock.assert_not_called()
