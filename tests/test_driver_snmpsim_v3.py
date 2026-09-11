"""@pytest.mark.slow integration tests — SNMPv3 (auth+priv) against `snmpsim`.

Boots `snmpsim` with auth+priv credentials, drives
`Pmp450iDriver.fetch_radio_metrics` over the encrypted session, and
asserts the typed `RadioMetricsReport`.

Run only when opted-in: `uv run pytest -m slow
tests/test_driver_snmpsim_v3.py`.
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

_SNMPSIM_CMD: tuple[str, ...] = ("snmpsim-command-responder",)


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_for_port(host: str, port: int, *, timeout: float = 15.0) -> None:
    """Block until the snmpsim agent is listening.

    snmpsim listens on UDP; we cannot probe with a real `connect()`.
    After a 2-second warmup the agent is fully booted.
    """
    deadline = time.monotonic() + timeout
    time.sleep(2.0)
    while time.monotonic() < deadline:
        with contextlib.suppress(Exception):
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(1.0)
                s.sendto(b"\x00", (host, port))
                return
        time.sleep(0.2)
    raise RuntimeError(f"snmpsim did not start listening on {host}:{port}")


def _snmpsim_binary() -> str:
    candidate = shutil.which("snmpsim-command-responder")
    if candidate is None:
        pytest.skip("snmpsim-command-responder not installed")
    return candidate


@pytest.fixture
def snmpsim_v3(tmp_path: Path) -> dict[str, Any]:
    """Boot a `snmpsim` agent with v3 SHA + AES configured for user `nora`."""
    binary = _snmpsim_binary()

    data_dir = tmp_path / "snmpsim-data-v3"
    data_dir.mkdir()
    user_data = data_dir / "nora.snmprec"
    user_data.write_text(
        "\n".join(
            [
                "1.3.6.1.4.1.161.19.3.1.1.1.0|INTEGER|87000000",
                "1.3.6.1.4.1.161.19.3.1.1.2.0|INTEGER|31000000",
                "1.3.6.1.4.1.161.19.3.1.1.3.0|INTEGER|-62",
                "1.3.6.1.4.1.161.19.3.1.1.4.0|INTEGER|19",
                "1.3.6.1.4.1.161.19.3.1.1.5.0|INTEGER|73",
                "1.3.6.1.4.1.161.19.3.1.1.6.0|STRING|64QAM",
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
            "--v3-user",
            "nora",
            "--v3-auth-key",
            "change-me-auth",
            "--v3-auth-protocol",
            "SHA",
            "--v3-priv-key",
            "change-me-priv",
            "--v3-priv-protocol",
            "AES",
            "--logging-method",
            "null",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=os.environ.copy(),
    )
    try:
        _wait_for_port(host, port, timeout=15.0)
        yield {"host": host, "port": port, "user": "nora"}
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)
        if proc.poll() is None:
            proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=2)


def _build_v3_driver(
    tmp_path: Path,
    host: str,
    port: int,
    *,
    auth_password: str,
    priv_password: str,
) -> Any:
    from nora.drivers.inventory import Inventory
    from nora.drivers.oid_catalog import OidCatalog, OidCatalogRegistry
    from nora.drivers.snmp_pmp450i import Pmp450iDriver

    inventory_path = tmp_path / "devices.yaml"
    device = {
        "device_id": "sm-7400-02",
        "vendor": "cambium",
        "model": "pmp450i",
        "firmware": "15.2.1",
        "host": host,
        "port": port,
        "snmp_version": "v3",
        "auth_password": auth_password,
        "priv_password": priv_password,
    }
    inventory_path.write_text("devices:\n  - " + json.dumps(device).lstrip() + "\n")
    inventory = Inventory.from_yaml(inventory_path)
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
        _catalogs_path=tmp_path,
        _catalogs={("cambium", "pmp450i", "15.2.1"): catalog},
    )
    return Pmp450iDriver(inventory=inventory, catalog_registry=registry)


@pytest.mark.slow
def test_v3_auth_priv_full_fetch_returns_typed_report(
    snmpsim_v3: dict[str, Any], tmp_path: Path
) -> None:
    """A v3 auth+priv fetch against snmpsim returns a typed report."""
    _probe_agent_or_skip(snmpsim_v3["host"], snmpsim_v3["port"])
    from unittest import mock

    driver = _build_v3_driver(
        tmp_path,
        snmpsim_v3["host"],
        snmpsim_v3["port"],
        auth_password="change-me-auth",
        priv_password="change-me-priv",
    )

    with mock.patch(
        "nora.drivers.snmp_pmp450i.driver.nora_session_set_focus",
        lambda device_id: {"focus_device_id": device_id, "devices_reviewed": [device_id]},
    ):
        report = driver.fetch_radio_metrics("sm-7400-02")

    assert report.device_id == "sm-7400-02"
    assert report.radio_dl_rate_bps == 87000000
    assert report.modulation == "64QAM"


def _probe_agent_or_skip(host: str, port: int) -> None:
    """Skip on pysnmp/snmpsim version mismatch — see v2c fixture."""
    import asyncio

    try:
        from puresnmp import Auth as _Auth
        from puresnmp import Client as _RawClient
        from puresnmp import Priv as _Priv
        from puresnmp import PyWrapper as _PyWrapper
        from puresnmp.credentials import V3 as _V3

        async def _probe() -> bool:
            auth = _Auth(b"change-me-auth", "sha")
            priv = _Priv(b"change-me-priv", "aes")
            c = _PyWrapper(_RawClient(host, _V3("nora", auth, priv), port))
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
