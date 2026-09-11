"""Shared pytest fixtures for the NORA test suite.

The `nora.llm.build_provider` factory caches its result in module-level
state so repeated calls return the SAME provider instance (per
`llm-provider-interface` spec). Each test that constructs a provider must
start from a clean cache, otherwise the singleton leaks across tests.

We also expose a hermetic `.env` fixture for the MCP server tests in
Phase 5/6 and several hermetic directory fixtures for the driver layer
(catalog + prompt + inventory).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import pytest

from nora import llm as llm_mod


@pytest.fixture(autouse=True)
def _reset_llm_factory_cache() -> None:
    """Reset the `build_provider` singleton between tests."""
    llm_mod._reset_factory_cache()
    yield
    llm_mod._reset_factory_cache()


# ---------------------------------------------------------------------------
# Driver-layer fixtures — catalogs, prompts, inventory
# ---------------------------------------------------------------------------


# A small but realistic OID catalog keyed by stable public object names
# (no MIB prose). Mirrors what `data/oid-catalogs/cambium/pmp450i/15.2.1.json`
# will look like for the v1 fixture shipped with the change.
_SAMPLE_CATALOG_PAYLOAD: dict[str, str] = {
    "radioDownlinkRate": "1.3.6.1.4.1.161.19.3.1.1.1.0",
    "radioUplinkRate": "1.3.6.1.4.1.161.19.3.1.1.2.0",
    "signalStrengthRx": "1.3.6.1.4.1.161.19.3.1.1.3.0",
    "signalStrengthTx": "1.3.6.1.4.1.161.19.3.1.1.4.0",
    "ssr": "1.3.6.1.4.1.161.19.3.1.1.5.0",
    "modulationMode": "1.3.6.1.4.1.161.19.3.1.1.6.0",
}

# Deterministic key for HMAC verification in tests. NOT for production.
SAMPLE_CATALOG_KEY: str = "test-catalog-signing-key-do-not-use-in-prod"


@pytest.fixture
def tmp_catalogs_dir(tmp_path: Path) -> Path:
    """An empty catalogs directory tree under `tmp_path`."""
    d = tmp_path / "oid-catalogs"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def sample_catalog(
    tmp_catalogs_dir: Path,
) -> dict[str, Any]:
    """Write a signed catalog JSON file and return its payload + key.

    Returns a dict with three keys:

    * `path`  — Path to the on-disk catalog file.
    * `data`  — the parsed JSON payload (object name -> dotted OID).
    * `key`   — the deterministic HMAC signing key.

    The HMAC is computed over the canonicalised oids map (sort_keys=True,
    separators=(",", ":")) so it matches what `OidCatalogRegistry.verify_all`
    recomputes on boot. Keeping the canonicalisation identical on both
    ends avoids drift between the test fixture and the verifier.
    """
    vendor = "cambium"
    model = "pmp450i"
    firmware = "15.2.1"
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
        nora_session_journal_dir=tmp_path / "sessions",
        nora_session_trace_max_steps=3,
        nora_session_journal_enabled=True,
        nora_operator_alias="recall-op",
        nora_oid_catalogs_path=catalogs_dir,
        nora_devices_inventory_path=devices_file,
        nora_oid_catalog_signing_key=SAMPLE_CATALOG_KEY,
        nora_prompts_dir=prompts_dir,
    )
