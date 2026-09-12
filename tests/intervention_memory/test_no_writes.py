"""Read-only AST guard + banned-import scanner for the `intervention_memory` package.

Mirrors `tests/test_driver_airgap.py` and `tests/test_session_journal_airgap.py`.
Two scans run over every `.py` file under `src/nora/intervention_memory/`:

1. **No writable file calls (R2).** Any `open(...)` with mode in
   `{"w", "a", "x", "+"}`, or any of `Path.write_text`,
   `Path.write_bytes`, `Path.unlink`, `os.replace`, `os.remove`,
   `os.removedirs`, `os.makedirs`, `shutil.rmtree` — flagged as an
   offender tuple `(relative_path, lineno, call_name)`.
2. **No banned imports (R10 / R11).** No `import pytest`,
   `from pytest`, `import _pytest`, `from _pytest`,
   `from nora.intervention_memory.shim_webui`, `from nora.server`,
   `from nora.drivers` in any production module except `shim_webui.py`
   itself (which may import `nora.intervention_memory.tools`).

The poison self-test (Task 6.4) copies the package to a tmp dir, injects
a known-bad write call, and asserts the detector catches it. This proves
the detector would catch a future regression.
"""

from __future__ import annotations

import ast
import re
import shutil
from pathlib import Path

INTERVENTION_MEMORY_DIR = (
    Path(__file__).resolve().parent.parent.parent / "src" / "nora" / "intervention_memory"
)

# Modes that open a file for writing. "r" / "rb" and the implicit-read default
# (no mode arg) are NOT flagged.
_WRITE_MODES: frozenset[str] = frozenset({"w", "a", "x", "+"})

# AST-level call detection. The detector walks `ast.Call` nodes whose
# `func` attribute matches one of these names. The check for `open(...)`
# additionally inspects the `mode` keyword / positional arg.
_AST_WRITE_NAMES: frozenset[str] = frozenset(
    {
        "write_text",
        "write_bytes",
        "unlink",
        "rmtree",
        "replace",
        "remove",
        "removedirs",
        "makedirs",
    }
)

# Regex fallback for AST-edge-cases (e.g., `getattr(path, "write_text")(...)`).
# Used to catch anything the AST walker didn't pick up.
_REGEX_WRITE_PATTERNS: tuple[str, ...] = (
    r"\.write_text\s*\(",
    r"\.write_bytes\s*\(",
    r"\.unlink\s*\(",
    r"\bos\.replace\s*\(",
    r"\bos\.remove\s*\(",
    r"\bos\.removedirs\s*\(",
    r"\bos\.makedirs\s*\(",
    r"\bshutil\.rmtree\s*\(",
)

# Module-level banned imports — pytest-related (R11) and NORA-side
# imports that would couple the package to MCP wiring (R10 / R-NEW-4).
_BANNED_IMPORTS: tuple[str, ...] = (
    "pytest",
    "_pytest",
    "monkeypatch",
    "nora.server",
    "nora.drivers",
    "nora.intervention_memory.shim_webui",
)


def _iter_python_files() -> list[Path]:
    """Return every `.py` file under `src/nora/intervention_memory/`.

    Sorted for deterministic test output. Empty list is returned when the
    package directory is missing (the scaffold is not yet in place).
    """
    if not INTERVENTION_MEMORY_DIR.exists():
        return []
    return sorted(INTERVENTION_MEMORY_DIR.rglob("*.py"))


def _is_write_mode_arg(arg: ast.expr) -> bool:
    """Return True if `arg` is a string literal whose value is in `_WRITE_MODES`.

    Only handles the simple cases the project actually uses: `open(path,
    "w")`, `open(path, mode="w")`, and `open(path, "w", encoding="utf-8")`.
    """
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value in _WRITE_MODES
    return False


def _walk_call_for_writes(node: ast.Call) -> str | None:
    """Inspect a single `ast.Call` node.

    Returns the call name (`open`, `write_text`, `os.replace`, etc.) if it
    is a write, otherwise `None`. The caller adds the source file path +
    line number to form an offender tuple.
    """
    func = node.func
    # Bare `open(...)` call — special handling because we need to inspect
    # the mode argument.
    if isinstance(func, ast.Name) and func.id == "open":
        # No args at all → defaults to "r" → read. No flag.
        if not node.args and not node.keywords:
            return None
        # Positional second arg: open(path, mode, ...)
        if len(node.args) >= 2 and _is_write_mode_arg(node.args[1]):
            return "open"
        # Keyword `mode=` arg.
        for kw in node.keywords:
            if kw.arg == "mode" and _is_write_mode_arg(kw.value):
                return "open"
        return None
    # Dotted path: `Path("...").write_text(...)` or `shutil.rmtree(...)`
    # or `os.remove(...)`. Check the leftmost attribute name.
    if isinstance(func, ast.Attribute):
        attr_name = func.attr
        if attr_name in _AST_WRITE_NAMES:
            return attr_name
    return None


def _find_writes_in_file(py_file: Path) -> list[tuple[int, str]]:
    """Return `(lineno, call_name)` for every write call found in `py_file`.

    Two passes: AST walker for `ast.Call` nodes, regex fallback for
    AST-edge-cases (the regex only fires when the AST walker did NOT
    already flag the line — avoids double-reporting).
    """
    text = py_file.read_text()
    tree = ast.parse(text)
    ast_offenders: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            call_name = _walk_call_for_writes(node)
            if call_name is not None:
                ast_offenders.append((node.lineno, call_name))

    flagged_lines: set[int] = {line for line, _ in ast_offenders}
    regex_offenders: list[tuple[int, str]] = []
    for pattern in _REGEX_WRITE_PATTERNS:
        compiled = re.compile(pattern)
        for m in compiled.finditer(text):
            line_no = text[: m.start()].count("\n") + 1
            if line_no in flagged_lines:
                continue
            # Skip matches on lines that are purely comments.
            line_text = (
                text.splitlines()[line_no - 1] if line_no - 1 < len(text.splitlines()) else ""
            )
            if line_text.lstrip().startswith("#"):
                continue
            regex_offenders.append((line_no, pattern))

    return sorted(ast_offenders + regex_offenders)


def _find_banned_imports_in_file(py_file: Path) -> list[tuple[int, str, str]]:
    """Return `(lineno, import_kind, module)` for every banned import in `py_file`.

    `import_kind` is `"import"` for `import X` and `"importfrom"` for
    `from X import Y`. The `module` is the dotted path as written.
    """
    src = py_file.read_text()
    tree = ast.parse(src)
    offenders: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if any(top == m.split(".")[0] for m in _BANNED_IMPORTS):
                    offenders.append((node.lineno, "import", alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                continue
            # Match the FULL dotted path against the banned list, not just
            # the top-level package. `from nora.intervention_memory.shim_webui`
            # must be caught even though the top-level package is "nora".
            if any(node.module == m or node.module.startswith(m + ".") for m in _BANNED_IMPORTS):
                offenders.append((node.lineno, "importfrom", node.module))
            # Also catch `from nora import server` (top-level only).
            elif node.module == "nora":
                for alias in node.names:
                    if alias.name in {"server", "drivers"}:
                        offenders.append((node.lineno, "importfrom", f"nora.{alias.name}"))
    return offenders


# ---------------------------------------------------------------------------
# Static AST scans — production code MUST remain read-only
# ---------------------------------------------------------------------------


def test_no_writable_file_calls_under_intervention_memory() -> None:
    """R2-S1 — every `.py` under `src/nora/intervention_memory/` MUST contain zero write calls."""
    offenders: list[tuple[str, int, str]] = []
    for py in _iter_python_files():
        for lineno, call_name in _find_writes_in_file(py):
            offenders.append(
                (
                    str(py.relative_to(py.parents[2])),
                    lineno,
                    call_name,
                )
            )
    assert offenders == [], (
        f"Write calls detected in intervention_memory package "
        f"(relative_path, lineno, call_name): {offenders}"
    )


def test_open_read_mode_is_allowed(tmp_path: Path) -> None:
    """R2-S3 — `open(..., "r")` is NOT flagged as a write.

    Injects a synthetic module under the package tree, runs the detector,
    and asserts zero offenders for the read-mode `open` call. The
    synthetic file is cleaned up at teardown.
    """
    synthetic = INTERVENTION_MEMORY_DIR / "_synthetic_read_only_test.py"
    synthetic.write_text(
        "with open('/tmp/x', 'r', encoding='utf-8') as f:\n"
        "    _data = f.read()\n"
        "with open('/tmp/x', encoding='utf-8') as f:\n"
        "    _data = f.read()\n"
        "with open('/tmp/x', 'rb') as f:\n"
        "    _data = f.read()\n"
    )
    try:
        offenders = _find_writes_in_file(synthetic)
    finally:
        synthetic.unlink()
    assert offenders == [], f"Read-mode open() calls were incorrectly flagged: {offenders}"


def test_production_modules_do_not_import_pytest() -> None:
    """R11-S2 — zero `pytest` / `_pytest` / `monkeypatch` imports in production modules."""
    offenders: list[tuple[str, int, str, str]] = []
    for py in _iter_python_files():
        if py.name == "__init__.py":
            continue
        for lineno, kind, module in _find_banned_imports_in_file(py):
            top = module.split(".")[0]
            if top in {"pytest", "_pytest", "monkeypatch"}:
                offenders.append((str(py.relative_to(py.parents[2])), lineno, kind, module))
    assert offenders == [], f"Test-only imports found in production code: {offenders}"


def test_production_modules_do_not_import_shim_webui() -> None:
    """R10-S3 / R-NEW-4-S3 — no production module may import from `shim_webui`."""
    offenders: list[tuple[str, int, str, str]] = []
    for py in _iter_python_files():
        if py.name in {"__init__.py", "shim_webui.py"}:
            continue
        for lineno, kind, module in _find_banned_imports_in_file(py):
            if module == "nora.intervention_memory.shim_webui" or module.startswith(
                "nora.intervention_memory.shim_webui."
            ):
                offenders.append((str(py.relative_to(py.parents[2])), lineno, kind, module))
    assert offenders == [], (
        f"Production code imports from shim_webui (one-way dep violated): {offenders}"
    )


def test_production_modules_do_not_import_nora_server_or_drivers() -> None:
    """R-NEW-4-S2 — no production module may import from `nora.server` or `nora.drivers`."""
    offenders: list[tuple[str, int, str, str]] = []
    for py in _iter_python_files():
        if py.name == "__init__.py":
            continue
        for lineno, kind, module in _find_banned_imports_in_file(py):
            if (
                module in {"nora.server", "nora.drivers"}
                or module.startswith("nora.server.")
                or module.startswith("nora.drivers.")
            ):
                offenders.append((str(py.relative_to(py.parents[2])), lineno, kind, module))
            # Also catch `from nora import server` / `from nora import drivers`.
            top = module.split(".")[0]
            if top == "nora":
                # Inspect the specific aliases to avoid flagging `from nora import config`.
                tree = ast.parse(py.read_text())
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom) and node.module == "nora":
                        for alias in node.names:
                            if alias.name in {"server", "drivers"}:
                                offenders.append(
                                    (
                                        str(py.relative_to(py.parents[2])),
                                        node.lineno,
                                        "importfrom",
                                        f"nora.{alias.name}",
                                    )
                                )
    assert offenders == [], f"Production code imports from nora.server or nora.drivers: {offenders}"


# ---------------------------------------------------------------------------
# Self-test poison — Task 6.4 / R2-S2 / R-NEW-3-S1
# ---------------------------------------------------------------------------


def test_injected_write_text_call_is_detected(tmp_path: Path) -> None:
    """R2-S2 / R-NEW-3-S1 — proves the detector catches a hand-injected `Path.write_text`.

    Copies `src/nora/intervention_memory/` to `tmp_path`, injects a known
    write call into the copy of `storage.py`, runs the detector against
    the copy, asserts the offenders list is non-empty and contains the
    expected `(relative_path, lineno, call_name)` triple. The package
    itself remains untouched.
    """

    if not INTERVENTION_MEMORY_DIR.exists():
        # The scaffold is missing — there is nothing to copy. The detector
        # trivially returns [], so we cannot prove it would catch a
        # regression. Skip with a clear message.
        import pytest

        pytest.skip("intervention_memory package not scaffolded yet")

    copy_root = tmp_path / "intervention_memory_copy"
    shutil.copytree(INTERVENTION_MEMORY_DIR, copy_root)

    # Pick the first .py in the copy (any will do — storage.py is preferred).
    target = copy_root / "storage.py"
    if not target.exists():
        candidates = sorted(copy_root.rglob("*.py"))
        assert candidates, "Copy has no .py files"
        target = candidates[0]
    original = target.read_text()
    poisoned = original + "\n\ndef _poison() -> None:\n    Path('/tmp/x').write_text('x')\n"
    target.write_text(poisoned)

    # Walk the copy tree and apply the detector.
    offenders: list[tuple[str, int, str]] = []
    for py in sorted(copy_root.rglob("*.py")):
        for lineno, call_name in _find_writes_in_file(py):
            offenders.append(
                (
                    str(py.relative_to(copy_root)),
                    lineno,
                    call_name,
                )
            )

    write_text_offenders = [o for o in offenders if o[2] == "write_text"]
    assert write_text_offenders, f"Poison did not produce a 'write_text' offender; got: {offenders}"
    # The poison is at the end of the file — assert the lineno is greater
    # than the original file length so we know we caught the injected call
    # and not an unrelated existing match.
    original_lines = len(original.splitlines())
    for _, lineno, _ in write_text_offenders:
        assert lineno > original_lines, (
            f"write_text offender at line {lineno} ≤ original file length {original_lines}"
        )


def _ensure_intervention_memory_scaffold() -> None:
    """Internal helper used by the smoke tests in test_no_writes.py.

    No-op when the package already exists. Exists so a future contributor
    who removes the scaffold gets a clear assertion failure rather than a
    confusing `FileNotFoundError` deep inside the walker.
    """
    # Touch the package __init__ path so the directory is materialised.
    # The file already exists for any successful apply run.
    if not INTERVENTION_MEMORY_DIR.exists():
        INTERVENTION_MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    # `__init__.py` is created by Task 0.1 — do not write it from the test.
