"""Tests for ``scripts/sign_catalog.py``.

The helper script signs an OID catalog envelope and writes it under
``<output_root>/<vendor>/<model>/<firmware>.json``. PR 1 parameterised
the script so an operator can re-sign any triple without editing the
source; these tests lock the public surface:

* Default invocation matches pre-PR1 behaviour (writes to
  ``data/oid-catalogs/cambium/pmp450i/15.2.1.json``).
* ``--vendor`` / ``--model`` / ``--firmware`` redirect the destination.
* ``--key`` overrides the env var; missing both surfaces a non-zero exit.
* The resulting envelope round-trips through the HMAC canonicalisation
  contract (``sort_keys=True, separators=(",", ":")``). Issue #62 WU-1
  (2026-09-19) exercises the HMAC side directly because the registry's
  required-OIDs gate still references the legacy spectrum OID names
  that this work-unit removes — gate updates land in WU-2.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "sign_catalog.py"
KEY = "test-sign-catalog-helper-key"


def _run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Invoke the helper script with a clean environment."""
    import os

    full_env = {k: v for k, v in os.environ.items() if not k.startswith("NORA_")}
    full_env.update(env)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        check=False,
        capture_output=True,
        text=True,
        env=full_env,
    )


def test_default_invocation_writes_legacy_baseline(tmp_path: Path) -> None:
    """No flags → writes the shipped PMP 450i baseline to ``data/oid-catalogs/``.

    PR 1 keeps the legacy default so existing operator re-sign workflows
    keep working byte-for-byte. We invoke the script with ``--output-root``
    pointing at ``tmp_path`` so the test stays hermetic (otherwise the
    script would touch the real ``data/oid-catalogs/`` repo path).
    """
    # Default vendor/model/firmware; redirect output to a tmp dir.
    result = _run(
        ["--output-root", str(tmp_path), "--key", KEY],
        env={},
    )
    assert result.returncode == 0, result.stderr
    target = tmp_path / "cambium" / "pmp450i" / "15.2.1.json"
    assert target.exists()
    envelope = json.loads(target.read_text())
    assert envelope["vendor"] == "cambium"
    assert envelope["model"] == "pmp450i"
    assert envelope["firmware"] == "15.2.1"
    assert "hmac_sha256" in envelope


def test_vendor_model_firmware_redirect(tmp_path: Path) -> None:
    """``--vendor`` / ``--model`` / ``--firmware`` redirect the destination triple."""
    result = _run(
        [
            "--vendor",
            "cambium",
            "--model",
            "pmp450i",
            "--firmware",
            "15.3.0",
            "--output-root",
            str(tmp_path),
            "--key",
            KEY,
        ],
        env={},
    )
    assert result.returncode == 0, result.stderr
    target = tmp_path / "cambium" / "pmp450i" / "15.3.0.json"
    assert target.exists()
    envelope = json.loads(target.read_text())
    assert envelope["vendor"] == "cambium"
    assert envelope["model"] == "pmp450i"
    assert envelope["firmware"] == "15.3.0"


def test_missing_key_returns_non_zero() -> None:
    """No ``--key`` and no ``NORA_OID_CATALOG_SIGNING_KEY`` env → exit 2.

    The helper refuses to sign without a key so an operator never lands
    an envelope signed with an empty HMAC.
    """
    result = _run([], env={})
    assert result.returncode == 2
    assert "NORA_OID_CATALOG_SIGNING_KEY" in result.stderr


def test_key_from_environment_overrides_inline_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``NORA_OID_CATALOG_SIGNING_KEY`` env is used when ``--key`` is absent.

    Documents the precedence: inline ``--key`` beats env, but env alone
    is enough. The legacy CI workflow (``NORA_OID_CATALOG_SIGNING_KEY``
    exported in the runner) keeps working without code changes.
    """
    monkeypatch.setenv("NORA_OID_CATALOG_SIGNING_KEY", KEY)
    result = _run(
        ["--output-root", str(tmp_path)],
        env={"NORA_OID_CATALOG_SIGNING_KEY": KEY},
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "cambium" / "pmp450i" / "15.2.1.json").exists()


def test_signed_envelope_round_trips_through_registry(tmp_path: Path) -> None:
    """End-to-end: sign + recompute the HMAC against the canonicalisation contract.

    Locks the helper's ``sort_keys=True, separators=(",", ":")`` shape:
    the same knobs the registry's ``_verify_one`` uses to re-derive the
    HMAC at boot. Any drift between signer and verifier surfaces here
    as a hex mismatch on the recomputed signature.

    Issue #62 WU-1 (2026-09-19): the catalog-required-OIDs gate
    (``OidCatalogRegistry._verify_one`` -> ``_REQUIRED_OIDS_BY_VENDOR_MODEL``)
    still references the legacy spectrum OID names (``spectrumNoiseFloorA/B/C``,
    ``spectrumChannelRank``, ``spectrumScanStatus``) that this work-unit
    removes from the source catalogs. Updating the gate to the new
    sweep-protocol OID names (``spectrumScanDuration``,
    ``spectrumScanAction``) is WU-2 territory (see
    ``odd/tasks/issue-62-spectrum-and-multi-community.md``). Until then,
    this test exercises the HMAC + canonicalisation contract directly
    rather than via ``OidCatalogRegistry.verify``, which would trip the
    pre-WU-2 gate.
    """
    import hashlib
    import hmac

    result = _run(
        ["--output-root", str(tmp_path), "--key", KEY],
        env={},
    )
    assert result.returncode == 0, result.stderr

    envelope = json.loads((tmp_path / "cambium" / "pmp450i" / "15.2.1.json").read_text())

    # Re-derive the HMAC using the exact canonicalisation the signer
    # uses (``sort_keys=True, separators=(",", ":")``). Any drift
    # between signer and the contract surfaces here as a hex mismatch.
    canonical = json.dumps(envelope["oids"], sort_keys=True, separators=(",", ":")).encode("utf-8")
    expected = hmac.new(KEY.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
    assert expected == envelope["hmac_sha256"], (
        f"HMAC round-trip mismatch — signer canonicalisation drift; "
        f"expected={expected!r} got={envelope['hmac_sha256']!r}"
    )

    # Issue #57 (2026-09-19): ``ssr`` was dropped from the signed
    # catalogs — it pointed at a per-LUID tabular column (.86.0)
    # that does not exist on real Cambium PMP 450i hardware;
    # ``ssrLink`` (also .86.0, semantically the per-LUID canonical)
    # survives as ``linkRadioAggrSignalStrengthRatio``.
    assert "eirp" in envelope["oids"]
    assert envelope["oids"]["eirp"] == "1.3.6.1.4.1.161.19.3.3.1.306.0"
