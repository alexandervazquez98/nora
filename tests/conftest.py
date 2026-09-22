"""Shared pytest fixtures for the NORA test suite.

We expose hermetic directory fixtures for the driver layer (catalogs +
prompts + inventory) and a `hermetic_settings` factory that builds a
`Settings` instance bound to a fresh per-test tmp tree.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Driver-layer fixtures — catalogs, prompts, inventory
# ---------------------------------------------------------------------------


# A small but realistic OID catalog keyed by stable public object names
# (no MIB prose). Mirrors what `data/oid-catalogs/cambium/pmp450i/15.2.1.json`
# will look like for the v1 fixture shipped with the change.
# PR 2 (slice 2 of `2026-09-13-pmp450i-production-surface`) extends
# the fixture with the four NEW summary OID names so the
# ``_REQUIRED_OIDS_BY_VENDOR_MODEL[("cambium", "pmp450i")]`` check
# accepts the sample. The test fixture is intentionally a STRICT
# subset of the production catalog (radio-metrics seed + summary
# additions + the legacy OIDs ``summaries.py`` reuses) so the
# verification path is exercised end-to-end.
# PR 3 (slice 3 of `2026-09-13-pmp450i-production-surface`) extends
# the fixture with the eight NEW SM-table / diagnostics OID names so
# the verification gate accepts the sample for the SM-table helpers
# in `nora.drivers.snmp_pmp450i.subscribers`.
_SAMPLE_CATALOG_PAYLOAD: dict[str, str] = {
    "radioDownlinkRate": "1.3.6.1.4.1.161.19.3.1.1.1.0",
    "radioUplinkRate": "1.3.6.1.4.1.161.19.3.1.1.2.0",
    "signalStrengthRx": "1.3.6.1.4.1.161.19.3.1.1.3.0",
    # Issue #54 (2026-09-19): ``signalStrengthTx`` dropped (broken on
    # production firmware — ``maxSMTxPwr`` engineering-only + tabular).
    # Sector-level active EIRP placeholder for the hermetic sample.
    "eirp": "1.3.6.1.4.1.161.19.3.1.1.4.0",
    "ssr": "1.3.6.1.4.1.161.19.3.1.1.5.0",
    "modulationMode": "1.3.6.1.4.1.161.19.3.1.1.6.0",
    # PR 2 — slice 2 read-summary additions.
    "apFirmwareVersion": "1.3.6.1.4.1.161.19.3.1.1.52.0",
    "subscribersCount": "1.3.6.1.4.1.161.19.3.1.1.60.0",
    "frameUtilizationDlPct": "1.3.6.1.4.1.161.19.3.1.1.53.0",
    "frameUtilizationUlPct": "1.3.6.1.4.1.161.19.3.1.1.54.0",
    # PR 3 — slice 3 SM-table additions.
    "smSessionUptime": "1.3.6.1.4.1.161.19.3.2.1.70.0",
    "smCinr": "1.3.6.1.4.1.161.19.3.2.1.71.0",
    "smLinkStatus": "1.3.6.1.4.1.161.19.3.2.1.72.0",
    "smLuid": "1.3.6.1.4.1.161.19.3.2.1.73.0",
    # PR 3 — slice 3 SM diagnostics additions.
    # Issue #54 (2026-09-19): ``smJitter`` (FSK-only linkAveJitter)
    # and ``smTxLevel`` (engineering-only maxSMTxPwr) dropped. OFDM-
    # correct per-LUID metrics added: vertical/horizontal CINR + SSR.
    "smSnrH": "1.3.6.1.4.1.161.19.3.2.1.80.0",
    "ssrLink": "1.3.6.1.4.1.161.19.3.2.1.81.0",
    "smRetransmits": "1.3.6.1.4.1.161.19.3.2.1.82.0",
    "smRxLevel": "1.3.6.1.4.1.161.19.3.2.1.83.0",
    # PR 4 — slice 4 spectrum-sweep additions.
    # Issue #62 (2026-09-19): the synthetic noise-floor scalars
    # were retired and replaced with the real Cambium sweep-
    # protocol OIDs (``whispBoxSpectrumScanDuration`` /
    # ``whispBoxSpectrumScanAction``).
    "spectrumScanDuration": "1.3.6.1.4.1.161.19.3.1.1.90.0",
    "spectrumScanAction": "1.3.6.1.4.1.161.19.3.1.1.91.0",
    # PR 4 — slice 4 RF-migration additions.
    "migrateCarrierFrequency": "1.3.6.1.4.1.161.19.3.1.1.95.0",
    "migratePriorCarrierFrequency": "1.3.6.1.4.1.161.19.3.1.1.96.0",
    # Issue #42 / `2026-09-15-register-device-mcp`: `sysDescr` is the
    # RFC 1213 OID that `register_device` reads for cheap reachability
    # validation. Adding it here keeps the hermetic `sample_catalog`
    # fixture aligned with the re-signed production catalogs (Task 6).
    "sysDescr": "1.3.6.1.2.1.1.1.0",
}

# Deterministic key for HMAC verification in tests. NOT for production.
SAMPLE_CATALOG_KEY: str = "test-catalog-signing-key-do-not-use-in-prod"


@pytest.fixture
def tmp_catalogs_dir(tmp_path: Path) -> Path:
    """An empty catalogs directory tree under `tmp_path`.

    Acts as the *operator* root in PR 1's two-root scan model
    (the matching built-in baseline lives at
    `src/nora/data/oid-catalogs/`; tests inject a hermetic built-in
    via the `tmp_builtin_root` fixture when they need it).
    """
    d = tmp_path / "oid-catalogs"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def tmp_builtin_root(tmp_path: Path) -> Path:
    """An empty built-in catalogs directory under `tmp_path`.

    Mirrors `tmp_catalogs_dir` but is wired up as the *built-in* root of
    `OidCatalogRegistry.verify(...)`. Tests that exercise the two-root
    scan write their hermetic baseline into this directory and pass it
    as `built_in_root=` while leaving `tmp_catalogs_dir` empty as the
    operator root (or vice versa). PR 1 ships a single built-in catalog
    (`cambium/pmp450i/15.2.1.json`) that operators never see directly —
    it's only reachable through the registry's built-in scan path.
    """
    d = tmp_path / "oid-catalogs-builtin"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def sample_catalog(
    tmp_catalogs_dir: Path,
    request: Any,
) -> dict[str, Any]:
    """Write a signed catalog JSON file and return its payload + key.

    Returns a dict with five keys:

    * `path`     — Path to the on-disk catalog file.
    * `data`     — the parsed JSON payload (object name -> dotted OID).
    * `key`      — the deterministic HMAC signing key.
    * `vendor`   — the catalog's vendor (default ``cambium``).
    * `model`    — the catalog's model (default ``pmp450i``).
    * `firmware` — the catalog's firmware string (default ``15.2.1``).

    Pass ``vendor=``, ``model=``, or ``firmware=`` kwargs (via
    ``pytest.mark.parametrize`` with ``indirect=True``) to write the
    catalog under a different triple. The HMAC is computed over the
    canonicalised oids map (sort_keys=True, separators=(",", ":"))
    so it matches what `OidCatalogRegistry.verify` recomputes on boot.
    Keeping the canonicalisation identical on both ends avoids drift
    between the test fixture and the verifier.
    """
    vendor = getattr(request, "param", {}).get("vendor", "cambium")
    model = getattr(request, "param", {}).get("model", "pmp450i")
    firmware = getattr(request, "param", {}).get("firmware", "15.2.1")
    target_dir = tmp_catalogs_dir / vendor / model
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{firmware}.json"

    canonical_body = json.dumps(
        _SAMPLE_CATALOG_PAYLOAD, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    sig = hmac.new(SAMPLE_CATALOG_KEY.encode(), canonical_body, hashlib.sha256).hexdigest()
    envelope = {
        "version": 1,
        "vendor": vendor,
        "model": model,
        "firmware": firmware,
        "oids": _SAMPLE_CATALOG_PAYLOAD,
        "hmac_sha256": sig,
    }
    path.write_text(json.dumps(envelope))
    return {
        "path": path,
        "data": _SAMPLE_CATALOG_PAYLOAD,
        "key": SAMPLE_CATALOG_KEY,
        "vendor": vendor,
        "model": model,
        "firmware": firmware,
    }


@pytest.fixture
def tmp_prompts_dir(tmp_path: Path) -> Path:
    """An empty prompts directory; tests add files inside it."""
    d = tmp_path / "prompts"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def sample_prompt(tmp_prompts_dir: Path) -> dict[str, Any]:
    """Write a valid `snmp_pmp450i.md` prompt and return its components."""
    name = "snmp_pmp450i"
    description = "Operator-facing instructions for the PMP 450i SNMP driver."
    body = (
        "# snmp_pmp450i tool\n"
        "\n"
        "Use `snmp_get_pmp450i_radio_metrics(device_id)` to fetch a typed\n"
        "`RadioMetricsReport`. Never echo private IPs, MACs, serials,\n"
        "hostnames, or credentials back to the user.\n"
    )
    target = tmp_prompts_dir / f"{name}.md"
    target.write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n{body}")
    return {"path": target, "name": name, "description": description, "body": body}


@pytest.fixture
def hermetic_settings(tmp_path: Path) -> Any:
    """A `Settings` instance bound to a fresh per-test tmp tree.

    Mirrors the Phase 2 journal hermetic fixture but adds the driver-layer
    paths (catalogs, devices, signing key, prompts).
    """
    from nora.config import Settings

    catalogs_dir = tmp_path / "oid-catalogs"
    catalogs_dir.mkdir(parents=True, exist_ok=True)
    devices_file = tmp_path / "devices.yaml"
    devices_file.write_text("# empty hermetic inventory\n")
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)

    return Settings(
        _env_file=None,
        _env_file_encoding=None,
        nora_oid_catalogs_path=catalogs_dir,
        nora_devices_inventory_path=devices_file,
        nora_oid_catalog_signing_key=SAMPLE_CATALOG_KEY,
        nora_prompts_dir=prompts_dir,
    )


@pytest.fixture
def sample_inventory(tmp_path: Path) -> Path:
    """A YAML inventory file with one v2c and one v3 PMP 450i device.

    RFC 5737 hosts (`192.0.2.x`) and `change-me` credentials — never
    real infrastructure values.
    """
    import yaml

    payload = {
        "devices": [
            {
                "device_id": "ap-7400-01",
                "vendor": "cambium",
                "model": "pmp450i",
                "firmware": "15.2.1",
                "host": "192.0.2.10",
                "snmp_version": "v2c",
                "community": "change-me-v2c",
            },
            {
                "device_id": "sm-7400-02",
                "vendor": "cambium",
                "model": "pmp450i",
                "firmware": "15.2.1",
                "host": "192.0.2.11",
                "snmp_version": "v3",
                "auth_password": "change-me-auth",
                "priv_password": "change-me-priv",
            },
        ]
    }
    path = tmp_path / "devices.yaml"
    path.write_text(yaml.safe_dump(payload))
    return path


# ---------------------------------------------------------------------------
# Shared HTTP MCP server (one per pytest-xdist worker)
# ---------------------------------------------------------------------------
#
# WU-#1a (test-perf follow-up): convert tool-surface integration tests from
# stdio subprocess boots to a shared HTTP MCP server so each test only pays
# the JSON-RPC round-trip, not the full Python interpreter + nora boot.
# pytest-xdist gives each worker its own process; with `scope="session"` plus
# the `worker_id` parameter, each worker boots exactly one HTTP server on a
# free OS-allocated port. Non-xdist runs (`worker_id == "master"`) also get
# a single server, so the fixture is transparent across modes.
#
# Stdio-specific tests (`test_subprocess_keeps_stdout_reserved_for_jsonrpc`,
# `test_subprocess_emits_structured_startup_log_on_stderr`, the
# `test_subprocess_*` family that asserts on stderr framing) MUST stay on
# stdio; this fixture is only for tests whose assertions are transport-
# agnostic tool-surface checks.


class McpHttpClient:
    """JSON-RPC over HTTP MCP, with auto-managed `Mcp-Session-Id` header.

    Each instance carries its own session; multiple clients against the same
    server do NOT share state, so concurrent tests on different workers (or
    serial tests within one worker) are isolated.
    """

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url
        self._session_id: str | None = None

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        id: int | None = None,
    ) -> dict[str, Any]:
        import httpx

        frame: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if id is not None:
            frame["id"] = id
        if params is not None:
            frame["params"] = params

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id is not None:
            headers["Mcp-Session-Id"] = self._session_id

        response = httpx.post(self._base_url, json=frame, headers=headers, timeout=5.0)
        if response.status_code not in (200, 202):
            raise RuntimeError(f"HTTP {response.status_code} for {method}: {response.text!r}")

        # Streamable-HTTP may reply with application/json OR text/event-stream.
        # Notifications (`method` without `id`) get 202 Accepted with empty
        # body — FastMCP's spec-compliant shape. Handle empty body first to
        # avoid `response.json()` blowing up on `b""`.
        if not response.content:
            body: dict[str, Any] = {}
        elif response.headers.get("content-type", "").startswith("application/json"):
            body = response.json()
        else:
            data_line = next(
                (ln for ln in response.text.splitlines() if ln.startswith("data:")),
                None,
            )
            if data_line is None:
                raise RuntimeError(f"SSE response without data: line; got: {response.text!r}")
            body = json.loads(data_line.split(":", 1)[1].strip())

        # Capture the server-assigned session ID for subsequent calls.
        if "Mcp-Session-Id" in response.headers:
            self._session_id = response.headers["Mcp-Session-Id"]

        return body

    def initialize(self) -> dict[str, Any]:
        return self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0.0.0"},
            },
            id=1,
        )

    def initialized(self) -> None:
        # Notification (no `id`); FastMCP returns 202 Accepted with empty body.
        self.request("notifications/initialized")

    def tools_list(self, *, id: int = 2) -> dict[str, Any]:
        return self.request("tools/list", id=id)


class McpStdioClient:
    """JSON-RPC over stdio MCP, newline-delimited JSON.

    Mirrors `McpHttpClient` but writes one JSON-RPC frame per line to stdin
    and reads one response line from stdout. Notifications (frames without
    `id`) are sent via `send_notification()` — they do NOT expect a reply
    and the implementation must not block waiting for one.

    A lock serializes stdin/stdout access so concurrent calls do not
    interleave frames (Popen pipes are not safe for parallel R/W).
    """

    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        self._proc = proc
        self._lock = threading.Lock()

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        id: int,
    ) -> dict[str, Any]:
        if id is None:
            raise ValueError(
                "request() is for JSON-RPC requests only; use send_notification() for notifications"
            )
        frame: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "id": id}
        if params is not None:
            frame["params"] = params

        line = (json.dumps(frame) + "\n").encode("utf-8")
        with self._lock:
            assert self._proc.stdin is not None
            self._proc.stdin.write(line)
            self._proc.stdin.flush()

            assert self._proc.stdout is not None
            response_line = self._proc.stdout.readline()
            if not response_line:
                raise RuntimeError(f"server closed stdout (no response) after method={method!r}")
            return json.loads(response_line.decode("utf-8"))

    def send_notification(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        frame: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            frame["params"] = params

        line = (json.dumps(frame) + "\n").encode("utf-8")
        with self._lock:
            assert self._proc.stdin is not None
            self._proc.stdin.write(line)
            self._proc.stdin.flush()
            # Notifications: do NOT read stdout; the server does not reply.

    def initialize(self) -> dict[str, Any]:
        return self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0.0.0"},
            },
            id=1,
        )

    def initialized(self) -> None:
        # Notification: server does not reply.
        self.send_notification("notifications/initialized")

    def tools_list(self, *, id: int = 2) -> dict[str, Any]:
        return self.request("tools/list", id=id)


def _free_port() -> int:
    """Ask the OS for an unused TCP port on 127.0.0.1."""
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


@pytest.fixture(scope="session")
def mcp_http_server(
    worker_id: str, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[str, int, subprocess.Popen[bytes]]]:
    """One MCP HTTP server per pytest-xdist worker; free port per worker.

    Yields `(host, port, proc)` so tests can build URLs and inspect
    `proc.stderr` if needed. Stderr is captured but not exposed via HTTP,
    so tests that assert on stderr framing MUST keep using stdio.
    """
    from nora.data import BUILTIN_BASELINE_SIGNING_KEY

    host = "127.0.0.1"
    port = _free_port()
    # Per-worker tmp dir so parallel workers don't collide on catalogs /
    # devices.yaml. pytest's `tmp_path_factory.mktemp` is itself safe under
    # xdist; we just disambiguate the prefix per worker.
    tmp = tmp_path_factory.mktemp(f"mcp_http_{worker_id}")
    (tmp / "catalogs").mkdir(exist_ok=True)
    (tmp / "devices.yaml").write_text("# empty hermetic inventory\n")

    project_root = Path(__file__).resolve().parent.parent
    venv_py = project_root / ".venv" / "bin" / "python"
    if not venv_py.exists():
        pytest.skip("venv python not present")
    py = str(venv_py)

    env = {
        **os.environ,
        "NORA_OID_CATALOG_SIGNING_KEY": BUILTIN_BASELINE_SIGNING_KEY,
        "NORA_OID_CATALOGS_PATH": str(tmp / "catalogs"),
        "NORA_DEVICES_INVENTORY_PATH": str(tmp / "devices.yaml"),
        "NORA_MCP_TRANSPORT": "http",
        "NORA_MCP_HOST": host,
        "NORA_MCP_PORT": str(port),
        "NORA_MCP_PATH": "/mcp",
    }

    proc = subprocess.Popen(
        [
            py,
            "-m",
            "nora.cli",
            "--transport=http",
            f"--host={host}",
            f"--port={port}",
            "--path=/mcp",
        ],
        cwd=str(project_root),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        bufsize=1,
    )

    # Wait up to 15s for the socket to bind. Polling `socket.create_connection`
    # is faster and more reliable than parsing the boot log line.
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            err = b""
            try:
                assert proc.stderr is not None
                err = proc.stderr.read() or b""
            except Exception:
                pass
            pytest.fail(
                f"nora-mcp exited before bind (rc={proc.returncode}); "
                f"stderr: {err.decode(errors='replace')!r}"
            )
        try:
            with socket.create_connection((host, port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)
    else:
        proc.kill()
        proc.wait()
        pytest.fail(f"nora-mcp never bound {host}:{port} within 15s")

    try:
        yield (host, port, proc)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        # Drain remaining stderr so the next stream read doesn't emit a
        # broken-pipe warning at the parent test runner.
        try:
            assert proc.stderr is not None
            proc.stderr.read()
        except Exception:
            pass


@pytest.fixture
def mcp_http_client(mcp_http_server: tuple[str, int, subprocess.Popen[bytes]]) -> McpHttpClient:
    """Per-test JSON-RPC client over the shared HTTP MCP server."""
    host, port, _proc = mcp_http_server
    return McpHttpClient(f"http://{host}:{port}/mcp")


@pytest.fixture(scope="session")
def mcp_stdio_server(
    worker_id: str, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[subprocess.Popen[bytes]]:
    """One nora-mcp subprocess per pytest-xdist worker, sharing stdio across tests.

    Mirrors `mcp_http_server` but uses stdio transport (the default for
    `nora-mcp`). Each worker boots exactly one `nora-mcp` process on a
    hermetic tmp tree; tests in the same worker share the proc and pay
    only the JSON-RPC round-trip cost per assertion.

    Cleanup: terminate the process, wait up to 5s, then SIGKILL on
    timeout. Drain stderr so the parent runner doesn't see a broken-
    pipe warning at fixture teardown.

    Tests that assert on stdout/stderr framing of the subprocess (e.g.
    `test_subprocess_keeps_stdout_reserved_for_jsonrpc`,
    `test_subprocess_emits_structured_startup_log_on_stderr`,
    `intervention_writer/test_stdio_smoke.py`) MUST NOT use this fixture;
    they need a fresh subprocess per test to assert on per-boot framing.
    """
    from nora.data import BUILTIN_BASELINE_SIGNING_KEY

    project_root = Path(__file__).resolve().parent.parent
    venv_py = project_root / ".venv" / "bin" / "python"
    if not venv_py.exists():
        pytest.skip("venv python not present")
    py = str(venv_py)

    # Per-worker tmp dir so parallel workers don't collide on catalogs /
    # devices.yaml.
    tmp = tmp_path_factory.mktemp(f"mcp_stdio_{worker_id}")
    (tmp / "catalogs").mkdir(exist_ok=True)
    (tmp / "devices.yaml").write_text("# empty hermetic inventory\n")

    env = {
        **os.environ,
        "NORA_OID_CATALOG_SIGNING_KEY": BUILTIN_BASELINE_SIGNING_KEY,
        "NORA_OID_CATALOGS_PATH": str(tmp / "catalogs"),
        "NORA_DEVICES_INVENTORY_PATH": str(tmp / "devices.yaml"),
        "NORA_MCP_TRANSPORT": "stdio",
    }

    proc = subprocess.Popen(
        [
            py,
            "-m",
            "nora.cli",
            "--transport=stdio",
        ],
        cwd=str(project_root),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        bufsize=0,
    )

    # Wait for the server to be ready by probing `initialize`. FastMCP's
    # stdio server is "ready" the moment the subprocess boots; we just
    # need to make sure it didn't crash on startup. Poll briefly with
    # `initialize`; if it responds with id=1 (the request id we sent),
    # the server is up.
    #
    # NOTE: if `initialize` returns a JSON-RPC NOTIFICATION (no `id`
    # field) instead of a response, the server crashed mid-handshake.
    # The FastMCP ``exception_handler`` middleware emits
    # ``{"method": "notifications/message", "params": {"level": "error",
    # "data": "Internal Server Error"}}`` and the actual traceback lives
    # in the subprocess stderr. Reading that stderr in the failure path
    # is the only way the operator/CI sees the real cause.
    ready_deadline = time.monotonic() + 10.0
    while time.monotonic() < ready_deadline:
        if proc.poll() is not None:
            err = b""
            try:
                assert proc.stderr is not None
                err = proc.stderr.read() or b""
            except Exception:
                pass
            pytest.fail(
                f"nora-mcp exited before ready (rc={proc.returncode}); "
                f"stderr: {err.decode(errors='replace')!r}"
            )
        try:
            client = McpStdioClient(proc)
            init_reply = client.initialize()
            if "id" not in init_reply:
                # Server emitted a notification instead of a reply.
                # Drain stderr to surface the traceback.
                err = b""
                try:
                    assert proc.stderr is not None
                    err = proc.stderr.read() or b""
                except Exception:
                    pass
                proc.kill()
                proc.wait()
                pytest.fail(
                    f"nora-mcp boot returned an error notification instead of "
                    f"an initialize reply: {init_reply!r}; "
                    f"stderr: {err.decode(errors='replace')!r}"
                )
            break
        except Exception:
            time.sleep(0.1)
    else:
        proc.kill()
        proc.wait()
        pytest.fail("nora-mcp never responded to initialize within 10s")

    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        try:
            assert proc.stderr is not None
            proc.stderr.read()
        except Exception:
            pass
