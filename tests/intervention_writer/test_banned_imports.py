"""Banned-imports AST guard — W7 of the writer contract.

The writer package is the SINGLE place in `src/nora/` allowed to write
to `nora_interventions_dir`. The AST guard walks every `.py` under
`src/nora/intervention_writer/` and refuses imports of:

- `pytest`, `_pytest`, `monkeypatch` (test-only deps — R11 boundary)
- `nora.server` (would couple writer to MCP wiring — W7 boundary)
- `nora.drivers` (would couple writer to SNMP — W7 boundary)
- `nora.intervention_memory.shim_webui` (reader-side mirror shim — W7 boundary)

Allowed inbound deps (the one-way dep):

- `nora.intervention_memory.models` — `InterventionMemoryRecord` schema
- `nora.intervention_memory.sanitize` — `_sanitize_value`, `Sanitizer`
- `nora.config` — `Settings`
- `nora.sanitizer` — `Sanitizer`

The poison self-test (Task 4.3 / R-NEW-3-S1) injects `import nora.server`
into a copy of the writer package and asserts the detector catches it.
This proves the detector would catch a future regression.
"""

from __future__ import annotations

import ast
import shutil
from pathlib import Path

# Module-level banned imports — symmetric with the reader's R11 list
# (see `tests/intervention_memory/test_no_writes.py`).
_BANNED_IMPORTS: tuple[str, ...] = (
    "pytest",
    "_pytest",
    "monkeypatch",
    "nora.server",
    "nora.drivers",
    "nora.intervention_memory.shim_webui",
)

WRITER_DIR = Path(__file__).resolve().parent.parent.parent / "src" / "nora" / "intervention_writer"


def _iter_python_files() -> list[Path]:
    """Return every `.py` under `src/nora/intervention_writer/` (sorted)."""
    if not WRITER_DIR.exists():
        return []
    return sorted(WRITER_DIR.rglob("*.py"))


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
            # Match the FULL dotted path against the banned list, not
            # just the top-level package. `from nora.server import X`
            # and `import nora.server` both caught.
            if any(node.module == m or node.module.startswith(m + ".") for m in _BANNED_IMPORTS):
                offenders.append((node.lineno, "importfrom", node.module))
            # Also catch `from nora import server` / `from nora import drivers`.
            elif node.module == "nora":
                for alias in node.names:
                    if alias.name in {"server", "drivers"}:
                        offenders.append((node.lineno, "importfrom", f"nora.{alias.name}"))
    return offenders


# ---------------------------------------------------------------------------
# AST scan — production writer code MUST NOT import banned modules
# ---------------------------------------------------------------------------


def test_writer_has_no_banned_imports() -> None:
    """W7 — zero `pytest` / `_pytest` / `monkeypatch` / `nora.server` /
    `nora.drivers` / `nora.intervention_memory.shim_webui` imports under
    `src/nora/intervention_writer/`.
    """
    offenders: list[tuple[str, int, str, str]] = []
    for py in _iter_python_files():
        if py.name == "__init__.py":
            # `__init__.py` is allowed to re-export — it imports the
            # writer's own modules (which is fine). The `nora.server`
            # import is still banned; other writer modules are checked
            # in the same scan.
            for lineno, kind, module in _find_banned_imports_in_file(py):
                if any(module == m or module.startswith(m + ".") for m in _BANNED_IMPORTS):
                    offenders.append((str(py.relative_to(py.parents[2])), lineno, kind, module))
            continue
        for lineno, kind, module in _find_banned_imports_in_file(py):
            if any(module == m or module.startswith(m + ".") for m in _BANNED_IMPORTS):
                offenders.append((str(py.relative_to(py.parents[2])), lineno, kind, module))
    assert offenders == [], f"Banned imports found in writer package: {offenders}"


# ---------------------------------------------------------------------------
# Poison self-test — proves the detector catches a hand-injected bad import
# ---------------------------------------------------------------------------


def test_injected_nora_server_import_is_detected(tmp_path: Path) -> None:
    """W7 poison self-test — injects `import nora.server` into a copy of the
    writer package and asserts the detector catches it.

    Mirrors
    `tests/intervention_memory/test_no_writes.py::test_injected_write_text_call_is_detected`.
    The writer package itself remains untouched.
    """
    if not WRITER_DIR.exists():
        import pytest

        pytest.skip("intervention_writer package not scaffolded yet")

    copy_root = tmp_path / "intervention_writer_copy"
    shutil.copytree(WRITER_DIR, copy_root)

    # Pick the first .py in the copy; inject a known-bad import at the end.
    candidates = sorted(copy_root.rglob("*.py"))
    assert candidates, "Copy has no .py files"
    target = candidates[0]
    original = target.read_text()
    poisoned = original + "\n\nimport nora.server  # poison\n"
    target.write_text(poisoned)

    # Walk the copy tree and apply the detector.
    offenders: list[tuple[str, int, str, str]] = []
    for py in sorted(copy_root.rglob("*.py")):
        for lineno, kind, module in _find_banned_imports_in_file(py):
            if any(module == m or module.startswith(m + ".") for m in _BANNED_IMPORTS):
                offenders.append(
                    (
                        str(py.relative_to(copy_root)),
                        lineno,
                        kind,
                        module,
                    )
                )

    server_offenders = [o for o in offenders if o[3].startswith("nora.server")]
    assert server_offenders, f"Poison did not produce a 'nora.server' offender; got: {offenders}"
    # The poison is at the end of the file — assert the lineno is greater
    # than the original file length so we know we caught the injected
    # import and not an unrelated existing match.
    original_lines = len(original.splitlines())
    for _, lineno, _, _ in server_offenders:
        assert lineno > original_lines, (
            f"nora.server offender at line {lineno} ≤ original file length {original_lines}"
        )
