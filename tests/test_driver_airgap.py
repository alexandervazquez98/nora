"""Driver air-gap static + runtime scan — Driver-R7 / OidCatalog-R7.

Mirrors `tests/test_session_journal_airgap.py` over
`src/nora/drivers/` and `src/nora/prompts/`. The check is:

1. No banned imports (`requests`, `httpx`, `urllib.request`, `socket`,
   `ssl`, `http.client`).
2. No dotted-path attribute references to banned modules (catches
   inline `urllib.request.urlopen(...)`).
3. No `socket.create_connection` literals (catches direct egress).

Run via `pytest tests/test_driver_airgap.py`. The test runs the full
driver + prompt module surface without performing any real network
egress.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DRIVERS_DIR = PROJECT_ROOT / "src" / "nora" / "drivers"
PROMPTS_DIR = PROJECT_ROOT / "src" / "nora" / "prompts"

# Same banned list as the session-journal gate, with `socket.create_connection`
# added for the driver layer (where it would be the obvious bypass).
_BANNED_MODULES: tuple[str, ...] = (
    "requests",
    "httpx",
    "urllib.request",
    "socket",
    "ssl",
    "http.client",
    "aiohttp",
)


def _iter_python_files() -> list[Path]:
    files: list[Path] = []
    for root in (DRIVERS_DIR, PROMPTS_DIR):
        if not root.exists():
            continue
        files.extend(sorted(root.rglob("*.py")))
    return files


def _find_banned_imports(py_file: Path) -> list[tuple[int, str, str]]:
    """Return list of (lineno, import_type, module) for banned imports."""
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


def test_no_banned_imports_in_driver_layer() -> None:
    """No banned imports under `src/nora/drivers/` or `src/nora/prompts/`."""
    offenders: list[tuple[str, int, str, str]] = []
    for py in _iter_python_files():
        for lineno, kind, module in _find_banned_imports(py):
            offenders.append((str(py), lineno, kind, module))
    assert offenders == [], (
        f"Banned imports found in driver/prompts: "
        f"{[(path, line, kind, mod) for path, line, kind, mod in offenders]}"
    )


def test_resolver_path_is_in_ast_walked_set() -> None:
    """The new `drivers/resolver.py` module is covered by the static AST scan.

    The IP-direct resolution path introduced for #14 must not regress
    the air-gap: any banned import that lands in `resolver.py` must be
    caught by `_iter_python_files`. This test pins `drivers/resolver.py`
    to the walked set so a future refactor that narrows the rglob
    (e.g. an allow-list) cannot silently exclude the new module.
    """
    walked = {p.relative_to(PROJECT_ROOT).as_posix() for p in _iter_python_files()}
    assert "src/nora/drivers/resolver.py" in walked, (
        f"src/nora/drivers/resolver.py is not in the AST-walked set: "
        f"{sorted(p for p in walked if 'drivers' in p)}"
    )


def test_no_banned_dotted_attribute_paths() -> None:
    """Inline references to `urllib.request.urlopen(...)` etc. are banned."""
    offenders: list[tuple[str, int, str]] = []
    for py in _iter_python_files():
        text = py.read_text()
        for mod in _BANNED_MODULES:
            pattern = re.compile(rf"\b{re.escape(mod)}\b")
            for m in pattern.finditer(text):
                line_no = text[: m.start()].count("\n") + 1
                line_text = text.splitlines()[line_no - 1]
                if line_text.lstrip().startswith("#"):
                    continue
                offenders.append((str(py), line_no, mod))
    assert offenders == [], f"Dotted-path banned references found: {offenders}"


def test_no_socket_create_connection_literal() -> None:
    """No `socket.create_connection(...)` calls under the driver layer."""
    pattern = re.compile(r"\bsocket\.create_connection\b")
    offenders: list[tuple[str, int]] = []
    for py in _iter_python_files():
        text = py.read_text()
        for m in pattern.finditer(text):
            line_no = text[: m.start()].count("\n") + 1
            line_text = text.splitlines()[line_no - 1]
            if line_text.lstrip().startswith("#"):
                continue
            offenders.append((str(py), line_no))
    assert offenders == [], f"socket.create_connection literals found: {offenders}"


# ---------------------------------------------------------------------------
# Runtime mock-based assertion — Driver-R7-S2
# ---------------------------------------------------------------------------


def test_full_driver_path_does_not_call_banned_symbols(tmp_path: Path) -> None:
    """Driving the full driver surface does NOT call any banned symbol.

    Imports the public surface, mocks every banned symbol at its import
    site, runs an end-to-end `fetch_radio_metrics` against an in-process
    fake client, and asserts every mock was never called.
    """
    # Build a hermetic inventory + catalog for the run.
    import yaml

    from nora.drivers.inventory import Inventory
    from nora.drivers.oid_catalog import OidCatalog, OidCatalogRegistry
    from nora.drivers.snmp_pmp450i import Pmp450iDriver, RadioMetricsReport
    from nora.prompts.registry import PromptRegistry

    inv_path = tmp_path / "devices.yaml"
    inv_path.write_text(
        yaml.safe_dump(
            {
                "devices": [
                    {
                        "device_id": "ap-7400-01",
                        "vendor": "cambium",
                        "model": "pmp450i",
                        "firmware": "15.2.1",
                        "host": "192.0.2.10",
                        "snmp_version": "v2c",
                        "community": "change-me",
                    }
                ]
            }
        )
    )
    inventory = Inventory.from_yaml(inv_path)
    catalog = OidCatalog(
        vendor="cambium",
        model="pmp450i",
        firmware="15.2.1",
        oids={
            "radioDownlinkRate": "1.3.6.1.4.1.161.19.3.1.1.1.0",
            "radioUplinkRate": "1.3.6.1.4.1.161.19.3.1.1.2.0",
            "signalStrengthRx": "1.3.6.1.4.1.161.19.3.1.1.3.0",
            "signalStrengthTx": "1.3.6.1.4.1.161.19.3.1.1.4.0",
            "ssr": "1.3.6.1.4.1.161.19.3.1.1.5.0",
            "modulationMode": "1.3.6.1.4.1.161.19.3.1.1.6.0",
        },
    )
    registry = OidCatalogRegistry(
        _catalogs_path=tmp_path, _catalogs={("cambium", "pmp450i", "15.2.1"): catalog}
    )

    fake_report = RadioMetricsReport(
        device_id="ap-7400-01",
        fetched_at=__import__("datetime").datetime(2026, 1, 1, 12, 0, 0),
        firmware="15.2.1",
        radio_dl_rate_bps=54000000,
        radio_ul_rate_bps=21000000,
        rx_signal_dbm=-58,
        tx_signal_dbm=23,
        ssr=75,
        modulation="256QAM",
    )

    driver = Pmp450iDriver(
        inventory=inventory,
        catalog_registry=registry,
        client_factory=lambda d: _FakeClient(fake_report),
    )

    # Confirm the prompts registry boots without banned imports.
    prompts = PromptRegistry.scan(PROMPTS_DIR)
    assert "snmp_pmp450i" in prompts.names

    # Now run the driver with banned-import mocks armed.
    with (
        mock.patch("socket.socket") as socket_mock,
        mock.patch("urllib.request.urlopen") as urlopen_mock,
        mock.patch("httpx.get") as httpx_get_mock,
        mock.patch("httpx.post") as httpx_post_mock,
        mock.patch("ssl.SSLContext") as ssl_mock,
        mock.patch("socket.create_connection") as create_conn_mock,
    ):
        report = driver.fetch_radio_metrics("ap-7400-01")
        assert report.device_id == "ap-7400-01"

    socket_mock.assert_not_called()
    urlopen_mock.assert_not_called()
    httpx_get_mock.assert_not_called()
    httpx_post_mock.assert_not_called()
    ssl_mock.assert_not_called()
    create_conn_mock.assert_not_called()


class _FakeClient:
    """A SnmpClient-shaped fake that returns the canned report.

    Returns from `get_oid` are the wire values; `RadioMetricsReport.fold`
    coerces them into the typed fields.
    """

    def __init__(self, report: Any) -> None:
        self._report = report

    def get_oid(self, oid: str) -> str | int:
        return {
            "1.3.6.1.4.1.161.19.3.1.1.1.0": str(self._report.radio_dl_rate_bps),
            "1.3.6.1.4.1.161.19.3.1.1.2.0": str(self._report.radio_ul_rate_bps),
            "1.3.6.1.4.1.161.19.3.1.1.3.0": str(self._report.rx_signal_dbm),
            "1.3.6.1.4.1.161.19.3.1.1.4.0": str(self._report.tx_signal_dbm),
            "1.3.6.1.4.1.161.19.3.1.1.5.0": str(self._report.ssr),
            "1.3.6.1.4.1.161.19.3.1.1.6.0": self._report.modulation,
        }[oid]

    def walk(self, base_oid: str) -> list[tuple[str, str | int]]:
        return []

    def close(self) -> None:
        return None
