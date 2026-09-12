"""AST guard: server.py and __main__.py must not import LLM/journal modules.

This is a structural regression test for `nora-mcp-thin-split`. After the
thin split, `src/nora/server.py` and `src/nora/__main__.py` MUST NOT
import from `nora.llm`, `nora.core.session_journal`, or any
`nora.core.session_*` module. The fastMCP server has no LLM provider and
no SessionJournal — the boot sequence lives in `src/nora/cli.py`.

The AST scan is intentionally narrow (only the two top-level entry
modules). Driver / intervention_memory modules are out of scope.
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SERVER_PY = PROJECT_ROOT / "src" / "nora" / "server.py"
MAIN_PY = PROJECT_ROOT / "src" / "nora" / "__main__.py"

# Banned import fragments. Any of these appearing in `import ...` or
# `from ... import ...` in the scanned files fails the test.
_BANNED_FRAGMENTS: tuple[str, ...] = (
    "nora.llm",
    "nora.core.session_journal",
    "nora.core.session_models",
    "nora.core.session_paths",
    "nora.core.session_redaction",
    "nora.core.session_rotation",
)


def _scan_for_banned_imports(py_file: Path) -> list[tuple[int, str, str]]:
    """Return a list of (lineno, kind, module) for every banned import in `py_file`."""
    src = py_file.read_text()
    tree = ast.parse(src)
    offenders: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if any(alias.name == frag or alias.name.startswith(frag + ".")
                       for frag in _BANNED_FRAGMENTS):
                    offenders.append((node.lineno, "import", alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                continue
            if any(node.module == frag or node.module.startswith(frag + ".")
                   for frag in _BANNED_FRAGMENTS):
                offenders.append((node.lineno, "importfrom", node.module))
    return offenders


def test_server_py_has_no_llm_or_journal_imports() -> None:
    """`src/nora/server.py` MUST NOT import from `nora.llm` or `nora.core.session_*`."""
    offenders = _scan_for_banned_imports(SERVER_PY)
    assert offenders == [], (
        f"server.py contains banned LLM/journal imports: {offenders}"
    )


def test_main_py_has_no_llm_or_journal_imports() -> None:
    """`src/nora/__main__.py` MUST NOT import from `nora.llm` or `nora.core.session_*`."""
    offenders = _scan_for_banned_imports(MAIN_PY)
    assert offenders == [], (
        f"__main__.py contains banned LLM/journal imports: {offenders}"
    )
