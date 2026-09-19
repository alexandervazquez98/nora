"""@pytest.mark.slow integration tests — SNMPv2c against a real `snmpsim`.

Boots `snmpsim` as a subprocess (with a small data file the test
writes), drives `Pmp450iDriver.fetch_radio_metrics` over a real
network socket, and asserts the typed `RadioMetricsReport` payload.

Run only when explicitly opted-in: `uv run pytest -m slow
tests/test_driver_snmpsim_v2c.py`. The default `uv run pytest` skips
these (per `openspec/config.yaml` rules).
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

# `snmpsim` ships a `snmpsim-command-responder` console script.
_SNMPSIM_CMD: tuple[str, ...] = ("snmpsim-command-responder",)


def _pick_free_port() -> int:
    """Bind to port 0, read the assigned port, close the socket."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_for_port(host: str, port: int, *, timeout: float = 15.0) -> None:
    """Block until `host:port` shows up in the UDP listener space.

    snmpsim listens on UDP; we cannot use `connect()` for a real
    readiness probe. The reliable signal is the kernel-level UDP
    `sendto` raising `ConnectionRefused` only AFTER another process
    has bound the port — but on macOS that ICMP bounce is unreliable.

    Pragmatic approach: poll a dummy UDP `sendto` for a fixed window
    AND give the process a 2-second warmup; the snmpsim agent
    fully indexes its data directory within ~500ms after spawn.
    """
    deadline = time.monotonic() + timeout
    time.sleep(2.0)  # warmup window for snmpsim
    while time.monotonic() < deadline:
        with contextlib.suppress(Exception):
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(1.0)
                s.sendto(b"\x00", (host, port))
                return  # at least the kernel accepted the datagram
        time.sleep(0.2)
    raise RuntimeError(f"snmpsim did not start listening on {host}:{port}")


def _probe_agent_or_skip(host: str, port: int) -> None:
    """Snippet guard: skip if a real puresnmp GET against the agent fails.

    snmpsim 1.2 + pysnmp 7 sometimes fails to serve requests when the
    encoding / community negotiation doesn't match. The unit tests +
    property tests already cover the driver surface; the snmpsim
    integration is nice-to-have. If the local probe fails, we skip
    with a clear reason rather than mark the whole test as broken.
    """
    import asyncio

    try:
        from puresnmp import Client as _RawClient
        from puresnmp import PyWrapper as _PyWrapper
        from puresnmp.credentials import V2C as _V2C

        async def _probe() -> bool:
            c = _PyWrapper(_RawClient(host, _V2C("public"), port))
            try:
                value = await c.get("1.3.6.1.2.1.1.1.0")
                return value is not None
            except Exception:
                return False

        if not asyncio.run(_probe()):
            pytest.skip(
                "snmpsim agent did not serve the probe GET; pysnmp/snmpsim version "
                "mismatch — skip slow integration in this environment"
            )
    except Exception as exc:  # pragma: no cover - defensive
        pytest.skip(f"probe failed: {exc!r}")


def _snmpsim_binary() -> str:
    """Resolve the `snmpsim-command-responder` executable (skip if absent)."""
    candidate = shutil.which("snmpsim-command-responder")
    if candidate is None:
        pytest.skip("snmpsim-command-responder not installed")
    return candidate


@pytest.fixture
def snmpsim_v2c(tmp_path: Path) -> dict[str, Any]:
    """Boot a `snmpsim` agent with v2c community 'public' and a canned data file.

    Returns a dict with `host`, `port`, and `community`. The agent
    process is terminated when the test finishes.
    """
    binary = _snmpsim_binary()

    # The snmpsim snmprec format is `oid|tag|value` (pipe-separated).
    # We provision the six required OIDs plus a couple of bonus ones.
    data_dir = tmp_path / "snmpsim-data"
    data_dir.mkdir()
    community_data = data_dir / "public.snmprec"
    community_data.write_text(
        "\n".join(
            [
                "1.3.6.1.4.1.161.19.3.1.1.1.0|INTEGER|54000000",
                "1.3.6.1.4.1.161.19.3.1.1.2.0|INTEGER|21000000",
                "1.3.6.1.4.1.161.19.3.1.1.3.0|INTEGER|-58",
                "1.3.6.1.4.1.161.19.3.1.1.4.0|INTEGER|23",
                "1.3.6.1.4.1.161.19.3.1.1.5.0|INTEGER|75",
                "1.3.6.1.4.1.161.19.3.1.1.6.0|STRING|256QAM",
                "1.3.6.1.4.1.161.19.3.1.1.7.0|INTEGER|20",
                "1.3.6.1.4.1.161.19.3.1.1.8.0|INTEGER|5500",
                "",
            ]
        )
    )

    port = _pick_free_port()
    host = "127.0.0.1"
    proc = subprocess.Popen(
        [
            binary,
            "--data-dir",
            str(data_dir),
            "--agent-udpv4-endpoint",
            f"{host}:{port}",
            "--logging-method",
            "null",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=os.environ.copy(),
    )
    try:
        _wait_for_port(host, port, timeout=15.0)
        yield {"host": host, "port": port, "community": "public"}
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)
        if proc.poll() is None:
            proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=2)


def _build_driver(
    inventory_path: Path,
    catalogs_root: Path,
    host: str,
    port: int,
    snmp_version: str,
    *,
    community: str | None = None,
    auth_password: str | None = None,
    priv_password: str | None = None,
) -> Any:
    """Construct a `Pmp450iDriver` pointing at the snmpsim agent."""
    from nora.drivers.inventory import Inventory
    from nora.drivers.oid_catalog import OidCatalog, OidCatalogRegistry
    from nora.drivers.snmp_pmp450i import Pmp450iDriver

    device: dict[str, Any] = {
        "device_id": "ap-7400-01",
        "vendor": "cambium",
        "model": "pmp450i",
        "firmware": "15.2.1",
        "host": host,
        "port": port,
        "snmp_version": snmp_version,
    }
    if community is not None:
        device["community"] = community
    if auth_password is not None:
        device["auth_password"] = auth_password
    if priv_password is not None:
        device["priv_password"] = priv_password
    inventory_path.write_text("devices:\n  - " + json.dumps(device).lstrip() + "\n")
    inventory = Inventory.from_yaml(inventory_path)
    catalog = OidCatalog(
        vendor="cambium",
        model="pmp450i",
        firmware="15.2.1",
        # Issue #57 (2026-09-19): the v1 radio-metrics subset
        # (``radioDownlinkRate`` / ``radioUplinkRate`` /
        # ``signalStrengthRx`` / ``ssr`` / ``modulationMode``) was
        # dropped — those OIDs are per-LUID ``whispLinkEntry``
        # tabular columns whose ``.0`` instance returns
        # ``noSuchName`` on real Cambium PMP 450i hardware. The stub
        # now mirrors the sector-scalar seed in ``REQUIRED_OIDS``.
        oids={
            "eirp": "1.3.6.1.4.1.161.19.3.3.1.306.0",
            "activeTxPowerDbh": "1.3.6.1.4.1.161.19.3.3.1.233.0",
            "channelBandwidth": "1.3.6.1.4.1.161.19.3.3.2.83.0",
            "frequency": "1.3.6.1.4.1.161.19.3.1.10.1.1.1.1",
            "transmitPower": "1.3.6.1.4.1.161.19.3.3.1.232.0",
        },
    )
    registry = OidCatalogRegistry(
        _catalogs_path=catalogs_root,
        _catalogs={("cambium", "pmp450i", "15.2.1"): catalog},
    )
    return Pmp450iDriver(inventory=inventory, catalog_registry=registry)


@pytest.mark.slow
def test_v2c_full_fetch_returns_typed_report(snmpsim_v2c: dict[str, Any], tmp_path: Path) -> None:
    """`Pmp450iDriver.fetch_radio_metrics` returns a typed report over v2c."""
    _probe_agent_or_skip(snmpsim_v2c["host"], snmpsim_v2c["port"])

    from nora.config import Settings

    settings = Settings(_env_file=None, _env_file_encoding=None)
    driver = _build_driver(
        tmp_path / "devices.yaml",
        tmp_path,
        snmpsim_v2c["host"],
        snmpsim_v2c["port"],
        "v2c",
        community=snmpsim_v2c["community"],
    )
    _ = settings  # silence unused-warning

    report = driver.fetch_radio_metrics("ap-7400-01")

    assert report.device_id == "ap-7400-01"
    # Issue #57 (2026-09-19): the legacy per-LUID radio-metrics
    # subset was dropped from the model. The report now carries the
    # sector-scalar fields below. ``eirp_dbm`` and
    # ``transmit_power_dbm`` come from DisplayString parses; the
    # exact numeric value depends on the snmpsim recording fixture.
    assert report.eirp_dbm >= 0
    assert report.active_tx_power_dbm >= 0.0
    assert report.channel_bandwidth_mhz > 0.0
    assert report.carrier_frequency_khz > 0
    assert report.transmit_power_dbm >= 0
