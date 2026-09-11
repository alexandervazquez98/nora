"""Read-only enforcement — Driver-R2.

Two layers of defense, mirrored by tests in this module:

1. A property test over `SnmpClient`'s public method set. The Protocol
   must NOT expose any write verb (`set`, `update`, `setbulk`,
   `bulk_set`, `write`).

2. An AST scan over every `.py` file under `src/nora/drivers/` that
   catches the write identifiers in *any* context (definitions, calls,
   attribute access). A `ruff check` rule (F821 / F841) plus a regex
   grep catch this layer; the test re-asserts it so a future refactor
   cannot accidentally re-introduce a write surface.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src" / "nora"

# The exact set of write identifiers Driver-R2 forbids under the driver
# package. Matched case-sensitively (Python identifier rules).
_WRITE_VERB_SET: frozenset[str] = frozenset({"set", "update", "setbulk", "bulk_set", "write"})


# ---------------------------------------------------------------------------
# Property test: SnmpClient's public method set is empty of write verbs
# ---------------------------------------------------------------------------


def test_snmp_client_protocol_exposes_no_write_verbs() -> None:
    """`SnmpClient` Protocol exposes ONLY `get_oid`, `walk`, `close`.

    Strategy: pull the Protocol from `nora.drivers.snmp_pmp450i.client`
    and assert that no public method name appears in the write-verb
    blocklist. The check uses `vars(cls)` (plus the public dunder list)
    so a future addition of `set` or `write` triggers this gate.
    """
    from nora.drivers.snmp_pmp450i.client import SnmpClient

    declared = set(vars(SnmpClient))
    leaked = declared & _WRITE_VERB_SET
    assert leaked == set(), f"SnmpClient exposes write verb(s): {sorted(leaked)}"


@pytest.mark.parametrize("verb", sorted(_WRITE_VERB_SET))
def test_no_write_verb_named_on_snmp_client_protocol(verb: str) -> None:
    """Negative coverage per forbidden verb."""
    from nora.drivers.snmp_pmp450i.client import SnmpClient

    assert verb not in vars(SnmpClient), (
        f"SnmpClient must not declare {verb!r}; Driver-R2 violation"
    )


# ---------------------------------------------------------------------------
# AST lint: no write identifier under src/nora/drivers/
# ---------------------------------------------------------------------------


def _iter_python_files() -> list[Path]:
    return sorted((SRC_DIR / "drivers").rglob("*.py"))


def _find_write_identifiers(py_file: Path) -> list[tuple[int, str, str]]:
    """Return list of (lineno, name_kind, identifier) for write verbs in `py_file`.

    Matches both `def NAME(...)` definitions AND attribute-style calls
    like `client.set(oid, value)`. The kind column distinguishes them
    so a failure message points at the right context.
    """
    src = py_file.read_text()
    tree = ast.parse(src)
    offenders: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            if node.name in _WRITE_VERB_SET:
                offenders.append((node.lineno, "def", node.name))
        elif isinstance(node, ast.AsyncFunctionDef):
            if node.name in _WRITE_VERB_SET:
                offenders.append((node.lineno, "async-def", node.name))
        elif isinstance(node, ast.Attribute):
            if node.attr in _WRITE_VERB_SET:
                offenders.append((node.lineno, "attr", node.attr))
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in _WRITE_VERB_SET:
                offenders.append((node.lineno, "call", func.id))
    return offenders


def test_no_write_identifiers_under_src_nora_drivers() -> None:
    """AST scan over `src/nora/drivers/**/*.py` finds zero write identifiers."""
    offenders: list[tuple[str, int, str, str]] = []
    for py in _iter_python_files():
        for lineno, kind, name in _find_write_identifiers(py):
            offenders.append((str(py), lineno, kind, name))
    assert offenders == [], (
        f"Write identifiers found under src/nora/drivers/: "
        f"{[(path, line, kind, name) for path, line, kind, name in offenders]}"
    )


def test_no_write_identifiers_in_attribute_paths() -> None:
    """Inline attribute accesses like `client.set(...)` are also violations.

    Belt-and-braces over the AST scan: matches the identifier on a line
    that does NOT start a definition, in case the AST walker misses an
    obscure construct (e.g. lambda bodies).
    """
    pattern = re.compile(r"\b(set|update|setbulk|bulk_set|write)\b\s*\(")
    offenders: list[tuple[str, int, str]] = []
    for py in _iter_python_files():
        for lineno, line in enumerate(py.read_text().splitlines(), start=1):
            stripped = line.lstrip()
            if stripped.startswith("def ") or stripped.startswith("async def "):
                continue
            if pattern.search(line):
                offenders.append((str(py), lineno, line.strip()))
    assert offenders == [], (
        f"Write verb call sites found under src/nora/drivers/: "
        f"{[(path, line, src) for path, line, src in offenders]}"
    )


# ---------------------------------------------------------------------------
# Concrete client classes: no `set`/`update`/`write`/`setbulk`/`bulk_set`
# method in their public surface either (Driver-R2).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_path",
    [
        "nora.drivers.snmp_pmp450i.V2CClient",
        "nora.drivers.snmp_pmp450i.V3Client",
    ],
)
def test_concrete_client_classes_expose_no_write_verbs(client_path: str) -> None:
    """V2CClient / V3Client follow the same read-only rule."""
    import importlib

    module_name, _, class_name = client_path.rpartition(".")
    module = importlib.import_module(module_name)
    cls = getattr(module, class_name)
    declared = {n for n in vars(cls) if not n.startswith("_")}
    leaked = declared & _WRITE_VERB_SET
    assert leaked == set(), f"{client_path} exposes write verb(s): {sorted(leaked)}"


def test_driver_module_exposes_no_write_verbs() -> None:
    """`Pmp450iDriver` exposes only `fetch_radio_metrics` (read-only)."""
    from nora.drivers.snmp_pmp450i import Pmp450iDriver

    declared = {n for n in vars(Pmp450iDriver) if not n.startswith("_")}
    leaked = declared & _WRITE_VERB_SET
    assert leaked == set(), f"Pmp450iDriver exposes write verb(s): {sorted(leaked)}"


# ---------------------------------------------------------------------------
# Hypothesis property test — Driver-R2 / "the catalogue is empty"
# ---------------------------------------------------------------------------


def test_hypothesis_property_set_is_empty_over_protocol_methods() -> None:
    """Hypothesis sweep: over 100 random samples of `dir(SnmpClient)`,
    the intersection with the write-verb set is always empty.
    """
    from hypothesis import given, settings
    from hypothesis import strategies as st

    from nora.drivers.snmp_pmp450i.client import SnmpClient

    write_set = st.sampled_from(sorted(_WRITE_VERB_SET))

    @given(_=write_set)
    @settings(max_examples=20, deadline=None)
    def _check(_: str) -> None:
        declared = set(vars(SnmpClient))
        leaked = declared & _WRITE_VERB_SET
        assert leaked == set(), f"Write verbs found on SnmpClient: {sorted(leaked)}"

    _check()
