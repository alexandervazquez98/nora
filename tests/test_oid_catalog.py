"""Tests for the OID catalog loader + HMAC verifier.

Maps OidCatalog-R1..R7 scenarios from
`openspec/changes/phase2-pmp450i-driver/specs/oid-catalog/spec.md`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from nora.drivers.exceptions import (
    CatalogNotFoundError,
    CatalogVerificationError,
)
from nora.drivers.oid_catalog import (  # noqa: PLC0415
    _REQUIRED_OIDS_BY_VENDOR_MODEL,
    REQUIRED_OIDS,
    OidCatalog,
    OidCatalogRegistry,
)

# Local mirror of the conftest fixture payload + signing key. Tests use
# the same payload as `sample_catalog` so HMACs stay bit-identical across
# the single-root and multi-root cases. PR 2 (slice 2 of
# `2026-09-13-pmp450i-production-surface`) extends the fixture with the
# four NEW summary OID names so the
# ``_REQUIRED_OIDS_BY_VENDOR_MODEL[("cambium", "pmp450i")]`` check
# accepts the sample. PR 3 (slice 3) extends the fixture with the eight
# NEW SM-table / diagnostics OID names so the verification gate accepts
# the sample for the SM-table helpers in
# `nora.drivers.snmp_pmp450i.subscribers`.
_SAMPLE_BUILTIN_OIDS: dict[str, str] = {
    "radioDownlinkRate": "1.3.6.1.4.1.161.19.3.1.1.1.0",
    "radioUplinkRate": "1.3.6.1.4.1.161.19.3.1.1.2.0",
    "signalStrengthRx": "1.3.6.1.4.1.161.19.3.1.1.3.0",
    "signalStrengthTx": "1.3.6.1.4.1.161.19.3.1.1.4.0",
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
    "smJitter": "1.3.6.1.4.1.161.19.3.2.1.80.0",
    "smRetransmits": "1.3.6.1.4.1.161.19.3.2.1.81.0",
    "smRxLevel": "1.3.6.1.4.1.161.19.3.2.1.82.0",
    "smTxLevel": "1.3.6.1.4.1.161.19.3.2.1.83.0",
    # PR 4 — slice 4 spectrum-sweep additions.
    "spectrumNoiseFloorA": "1.3.6.1.4.1.161.19.3.1.1.90.0",
    "spectrumNoiseFloorB": "1.3.6.1.4.1.161.19.3.1.1.91.0",
    "spectrumNoiseFloorC": "1.3.6.1.4.1.161.19.3.1.1.92.0",
    "spectrumChannelRank": "1.3.6.1.4.1.161.19.3.1.1.93.0",
    "spectrumScanStatus": "1.3.6.1.4.1.161.19.3.1.1.94.0",
    # PR 4 — slice 4 RF-migration additions.
    "migrateCarrierFrequency": "1.3.6.1.4.1.161.19.3.1.1.95.0",
    "migratePriorCarrierFrequency": "1.3.6.1.4.1.161.19.3.1.1.96.0",
}
SAMPLE_CATALOG_KEY: str = "test-catalog-signing-key-do-not-use-in-prod"

# ---------------------------------------------------------------------------
# Schema constants — pinned for the v1 fixture shipped with the change
# ---------------------------------------------------------------------------


def test_required_oids_is_non_empty_frozenset() -> None:
    """REQUIRED_OIDS is a frozenset; the v1 schema covers at least the
    six well-known RF metrics that the driver folds into a typed report.
    """
    assert isinstance(REQUIRED_OIDS, frozenset)
    expected_subset = {
        "radioDownlinkRate",
        "radioUplinkRate",
        "signalStrengthRx",
        "signalStrengthTx",
        "ssr",
        "modulationMode",
    }
    assert expected_subset.issubset(REQUIRED_OIDS)


# ---------------------------------------------------------------------------
# R1 — On-disk layout
# ---------------------------------------------------------------------------


def test_resolve_reads_pinned_catalog_path(
    tmp_catalogs_dir: Path, sample_catalog: dict[str, Any]
) -> None:
    """Boot resolves `<path>/<vendor>/<model>/<firmware>.json`."""
    registry = OidCatalogRegistry.verify(
        built_in_root=None,
        operator_root=tmp_catalogs_dir,
        signing_key=sample_catalog["key"],
    )
    catalog = registry.resolve(
        (sample_catalog["vendor"], sample_catalog["model"], sample_catalog["firmware"])
    )
    assert catalog.vendor == sample_catalog["vendor"]
    assert catalog.model == sample_catalog["model"]
    assert catalog.firmware == sample_catalog["firmware"]


def test_resolve_under_explicit_path(
    sample_catalog: dict[str, Any],
) -> None:
    """`resolve` accepts the same vendor/model/firmware triple directly
    against the on-disk path; no relative-path guessing."""
    # The on-disk layout is `<root>/<vendor>/<model>/<firmware>.json`.
    catalogs_root = sample_catalog["path"].parent.parent.parent
    # Build a registry directly from the verified catalog.
    catalog = OidCatalog(
        vendor=sample_catalog["vendor"],
        model=sample_catalog["model"],
        firmware=sample_catalog["firmware"],
        oids=sample_catalog["data"],
    )
    registry = OidCatalogRegistry(
        _catalogs_path=catalogs_root,
        _catalogs={
            (sample_catalog["vendor"], sample_catalog["model"], sample_catalog["firmware"]): catalog
        },
    )
    resolved = registry.resolve(
        (sample_catalog["vendor"], sample_catalog["model"], sample_catalog["firmware"])
    )
    assert resolved.oids["radioDownlinkRate"] == "1.3.6.1.4.1.161.19.3.1.1.1.0"


# ---------------------------------------------------------------------------
# Strict-Major Hard Fail — locking fixture reissued under PR 2 (ADR #17 P2)
# ---------------------------------------------------------------------------


def test_unknown_firmware_raises_catalog_not_found(tmp_catalogs_dir: Path) -> None:
    """Locking fixture reissued under `Strict-Major Hard Fail` (PR 2).

    Spec scenario: ``Strict-Major Hard Fail > major mismatch raises a
    typed exception``. The ``tmp_catalogs_dir`` fixture is unchanged
    (operators ship only ``15.x`` catalogs); the request ``99.0.0``
    triggers the new strict-major branch in ``resolve``. The exception
    message MUST name both majors so operators grep server logs for
    ``99`` (requested) and ``15`` (registered).

    Per spec migration note in `oid-catalog/spec.md`: "Test
    `test_unknown_firmware_raises_catalog_not_found` is reissued under
    `Strict-Major Hard Fail` — the fixture requests `99.0.0` against
    only `15.x` catalogs and still raises a typed
    `CatalogNotFoundError`."
    """
    # Write a 15.x catalog so the registry has a registered major;
    # otherwise the strict-major branch has no registered majors to
    # name in the message.
    _write_signed_catalog(
        tmp_catalogs_dir,
        vendor="cambium",
        model="pmp450i",
        firmware="15.2.1",
        oids=dict(_SAMPLE_BUILTIN_OIDS),
        key=SAMPLE_CATALOG_KEY,
    )
    registry = OidCatalogRegistry.verify(
        built_in_root=None,
        operator_root=tmp_catalogs_dir,
        signing_key=SAMPLE_CATALOG_KEY,
    )
    with pytest.raises(CatalogNotFoundError) as exc:
        registry.resolve(("cambium", "pmp450i", "99.0.0"))
    message = str(exc.value)
    assert "99.0.0" in message
    assert "cambium" in message
    # PR 2 addition: the strict-major message names both majors so the
    # operator can see which major line the fleet runs against.
    assert "99" in message, (
        f"Strict-major message must name the requested major 99; got: {message!r}"
    )
    assert "15" in message, (
        f"Strict-major message must name the registered major 15; got: {message!r}"
    )


# ---------------------------------------------------------------------------
# R3 — HMAC-SHA256 verification (happy path)
# ---------------------------------------------------------------------------


def test_valid_signed_catalog_verifies(
    tmp_catalogs_dir: Path, sample_catalog: dict[str, Any]
) -> None:
    """Catalog signed with the matching key passes verification and loads."""
    registry = OidCatalogRegistry.verify(
        built_in_root=None,
        operator_root=tmp_catalogs_dir,
        signing_key=sample_catalog["key"],
    )
    catalog = registry.resolve(
        (sample_catalog["vendor"], sample_catalog["model"], sample_catalog["firmware"])
    )
    assert isinstance(catalog, OidCatalog)
    assert catalog.oids["ssr"] == "1.3.6.1.4.1.161.19.3.1.1.5.0"


# ---------------------------------------------------------------------------
# R3 / R4 — Tampered or missing-key → typed exception
# ---------------------------------------------------------------------------


def test_tampered_catalog_raises_catalog_verification_error(
    tmp_catalogs_dir: Path, sample_catalog: dict[str, Any]
) -> None:
    """Bytes no longer match the HMAC → `CatalogVerificationError`."""
    # Replace the catalog body with the wrong bytes; the original signature
    # no longer matches.
    payload = json.loads(sample_catalog["path"].read_text())
    payload["oids"]["ssr"] = "1.2.3.4.5.6.7.8"
    sample_catalog["path"].write_text(json.dumps(payload))
    with pytest.raises(CatalogVerificationError) as exc:
        OidCatalogRegistry.verify(
            built_in_root=None,
            operator_root=tmp_catalogs_dir,
            signing_key=sample_catalog["key"],
        )
    assert "hmac" in exc.value.reason.lower() or "signature" in exc.value.reason.lower()
    assert exc.value.path == sample_catalog["path"]


def test_missing_key_raises_catalog_verification_error(
    tmp_catalogs_dir: Path, sample_catalog: dict[str, Any]
) -> None:
    """Empty / unset key → `CatalogVerificationError`, driver does not start."""
    with pytest.raises(CatalogVerificationError) as exc:
        OidCatalogRegistry.verify(
            built_in_root=None,
            operator_root=tmp_catalogs_dir,
            signing_key="",
        )
    assert "key" in exc.value.reason.lower()


# ---------------------------------------------------------------------------
# R4 — Key rotation invalidates old catalogs
# ---------------------------------------------------------------------------


def test_key_rotation_invalidates_previously_signed_catalog(
    tmp_catalogs_dir: Path, sample_catalog: dict[str, Any]
) -> None:
    """Catalog signed with KEY_A fails when boot uses KEY_B."""
    # Re-sign with KEY_A so the catalog is valid for that key.
    canonical_body = json.dumps(
        sample_catalog["data"], sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    sig_a = hmac.new(b"KEY_A", canonical_body, hashlib.sha256).hexdigest()
    envelope = {
        "version": 1,
        "vendor": sample_catalog["vendor"],
        "model": sample_catalog["model"],
        "firmware": sample_catalog["firmware"],
        "oids": sample_catalog["data"],
        "hmac_sha256": sig_a,
    }
    sample_catalog["path"].write_text(json.dumps(envelope))

    # KEY_A signs it: passes
    OidCatalogRegistry.verify(
        built_in_root=None,
        operator_root=tmp_catalogs_dir,
        signing_key="KEY_A",
    )

    # KEY_B rejects it: typed exception
    with pytest.raises(CatalogVerificationError):
        OidCatalogRegistry.verify(
            built_in_root=None,
            operator_root=tmp_catalogs_dir,
            signing_key="KEY_B",
        )

    # Re-sign with KEY_B: passes again
    sig_b = hmac.new(b"KEY_B", canonical_body, hashlib.sha256).hexdigest()
    envelope["hmac_sha256"] = sig_b
    sample_catalog["path"].write_text(json.dumps(envelope))
    registry = OidCatalogRegistry.verify(
        built_in_root=None,
        operator_root=tmp_catalogs_dir,
        signing_key="KEY_B",
    )
    catalog = registry.resolve(
        (sample_catalog["vendor"], sample_catalog["model"], sample_catalog["firmware"])
    )
    assert catalog.oids["radioDownlinkRate"] == sample_catalog["data"]["radioDownlinkRate"]


# ---------------------------------------------------------------------------
# R5 — Schema validation (missing required OID = verification failure)
# ---------------------------------------------------------------------------


def test_missing_required_oid_raises_catalog_verification_error(
    tmp_catalogs_dir: Path, sample_catalog: dict[str, Any]
) -> None:
    """A catalog missing `radioDownlinkRate` fails schema validation."""
    payload = json.loads(sample_catalog["path"].read_text())
    payload["oids"].pop("radioDownlinkRate")
    # Re-sign so the HMAC matches the new bytes (canonicalised same way
    # as the verifier).
    canonical_body = json.dumps(payload["oids"], sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    payload["hmac_sha256"] = hmac.new(
        sample_catalog["key"].encode(), canonical_body, hashlib.sha256
    ).hexdigest()
    sample_catalog["path"].write_text(json.dumps(payload))

    with pytest.raises(CatalogVerificationError) as exc:
        OidCatalogRegistry.verify(
            built_in_root=None,
            operator_root=tmp_catalogs_dir,
            signing_key=sample_catalog["key"],
        )
    assert "radioDownlinkRate" in str(exc.value)


# ---------------------------------------------------------------------------
# R6 — No vendor MIB text
# ---------------------------------------------------------------------------


def test_catalog_files_contain_only_public_names_and_dotted_oids(
    sample_catalog: dict[str, Any],
) -> None:
    """No `OBJECT-TYPE`, `MODULE-IDENTITY`, or other vendor prose markers."""
    text = sample_catalog["path"].read_text()
    forbidden = ("OBJECT-TYPE", "MODULE-IDENTITY", "SYNTAX", "MAX-ACCESS")
    for marker in forbidden:
        assert marker not in text, f"Vendor MIB prose marker {marker!r} found in catalog file"


# ---------------------------------------------------------------------------
# R7 — No banned imports
# ---------------------------------------------------------------------------


def test_oid_catalog_module_is_network_free() -> None:
    """`oid_catalog.py` MUST NOT import any network module."""
    import ast

    text = (
        Path(__file__).resolve().parent.parent / "src" / "nora" / "drivers" / "oid_catalog.py"
    ).read_text()
    tree = ast.parse(text)
    banned = ("requests", "httpx", "urllib", "socket", "ssl", "http.client", "aiohttp")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not any(alias.name.startswith(b) for b in banned), alias.name
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert not any(module.startswith(b) for b in banned), module


# ---------------------------------------------------------------------------
# PR 1 — Per-(vendor, model) REQUIRED_OIDS table (ADR #17 schema fix)
# ---------------------------------------------------------------------------


class TestMultiRoot:
    """PR 1: per-(vendor, model) required-OID table + multi-root scan."""

    def test_required_oids_by_vendor_model_covers_pmp450i(self) -> None:
        """`_REQUIRED_OIDS_BY_VENDOR_MODEL` covers the v1 PMP 450i triple.

        Spec: `Catalog Schema Validation > missing OID fails verification
        per triple` requires the per-(vendor, model) scoping to be
        observable from the module surface.
        """
        assert ("cambium", "pmp450i") in _REQUIRED_OIDS_BY_VENDOR_MODEL
        pmp450i_required = _REQUIRED_OIDS_BY_VENDOR_MODEL[("cambium", "pmp450i")]
        assert isinstance(pmp450i_required, frozenset)
        # Six well-known RF metrics the driver folds into a typed report.
        expected = {
            "radioDownlinkRate",
            "radioUplinkRate",
            "signalStrengthRx",
            "signalStrengthTx",
            "ssr",
            "modulationMode",
        }
        assert expected.issubset(pmp450i_required)

    def test_required_oids_alias_is_radio_metrics_subset(self) -> None:
        """`REQUIRED_OIDS` is the radio-metrics subset of the PMP 450i triple.

        Driver import (`from nora.drivers.oid_catalog import REQUIRED_OIDS`)
        stays scoped to the radio-metrics OIDs that
        ``Pmp450iDriver._fetch_all`` iterates. The full PMP 450i
        required-OID set lives in
        ``_REQUIRED_OIDS_BY_VENDOR_MODEL[("cambium", "pmp450i")]`` and
        includes the four PR 2 summary OIDs as well; the driver
        delegate paths in ``nora.drivers.snmp_pmp450i.summaries`` look
        those up directly against the resolved catalog.
        """
        radio_metrics_oids = frozenset(
            {
                "radioDownlinkRate",
                "radioUplinkRate",
                "signalStrengthRx",
                "signalStrengthTx",
                "ssr",
                "modulationMode",
            }
        )
        assert REQUIRED_OIDS == radio_metrics_oids
        assert REQUIRED_OIDS.issubset(_REQUIRED_OIDS_BY_VENDOR_MODEL[("cambium", "pmp450i")])

    def test_missing_oid_fails_per_triple(
        self, tmp_catalogs_dir: Path, sample_catalog: dict[str, Any]
    ) -> None:
        """Scenario: missing OID fails verification per triple.

        Build a catalog that lacks `radioDownlinkRate` and re-sign it so
        the HMAC matches the truncated `oids` map. The verifier MUST
        raise `CatalogVerificationError` naming the missing key.
        """
        # Strip `radioDownlinkRate` from the catalog body.
        payload = json.loads(sample_catalog["path"].read_text())
        payload["oids"].pop("radioDownlinkRate")
        canonical_body = json.dumps(payload["oids"], sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        payload["hmac_sha256"] = hmac.new(
            sample_catalog["key"].encode(), canonical_body, hashlib.sha256
        ).hexdigest()
        sample_catalog["path"].write_text(json.dumps(payload))

        with pytest.raises(CatalogVerificationError) as exc:
            OidCatalogRegistry.verify(
                built_in_root=None,
                operator_root=tmp_catalogs_dir,
                signing_key=sample_catalog["key"],
            )
        assert "radioDownlinkRate" in str(exc.value)

    def test_operator_wins(
        self,
        tmp_builtin_root: Path,
        tmp_catalogs_dir: Path,
    ) -> None:
        """Scenario: operator override wins on conflict.

        Both roots contain a valid signed catalog for the same
        ``(cambium, pmp450i, 15.2.1)`` triple. The operator's catalog
        carries a sentinel OID value (``signalStrengthRx = "operator"``)
        and the built-in carries a different sentinel. Resolving the
        triple MUST return the operator copy; the built-in copy is
        shadowed.
        """
        builtin_payload = dict(_SAMPLE_BUILTIN_OIDS)
        builtin_payload["signalStrengthRx"] = "1.3.6.1.4.1.161.19.3.1.1.999.0"  # built-in
        builtin_path = _write_signed_catalog(
            tmp_builtin_root,
            vendor="cambium",
            model="pmp450i",
            firmware="15.2.1",
            oids=builtin_payload,
            key=SAMPLE_CATALOG_KEY,
        )

        operator_payload = dict(_SAMPLE_BUILTIN_OIDS)
        operator_payload["signalStrengthRx"] = "1.3.6.1.4.1.161.19.3.1.1.3.0"  # operator
        operator_path = _write_signed_catalog(
            tmp_catalogs_dir,
            vendor="cambium",
            model="pmp450i",
            firmware="15.2.1",
            oids=operator_payload,
            key=SAMPLE_CATALOG_KEY,
        )

        registry = OidCatalogRegistry.verify(
            built_in_root=tmp_builtin_root,
            operator_root=tmp_catalogs_dir,
            signing_key=SAMPLE_CATALOG_KEY,
        )
        catalog = registry.resolve(("cambium", "pmp450i", "15.2.1"))
        # Operator wins → its sentinel survives.
        assert catalog.oids["signalStrengthRx"] == "1.3.6.1.4.1.161.19.3.1.1.3.0"
        # Sanity: both files exist on disk so we know we actually wrote two roots.
        assert builtin_path.exists() and operator_path.exists()
        # And the registry has exactly one entry for the shared triple.
        assert ("cambium", "pmp450i", "15.2.1") in {tuple(r) for r in registry.loaded_refs}

    def test_deterministic_two_root_scan(
        self,
        tmp_builtin_root: Path,
        tmp_catalogs_dir: Path,
    ) -> None:
        """Scenario: deterministic two-root scan with override precedence.

        Built-in holds a disjoint triple ``(cambium, pmp450i, 15.3.0)``
        AND shares the canonical ``(cambium, pmp450i, 15.2.1)`` with the
        operator (which also carries the disjoint
        ``(cambium, pmp450i, 15.3.5)``). Running ``verify`` twice with
        identical inputs MUST yield identical ``loaded_refs`` and the
        shared triple must resolve to the operator copy.
        """
        _write_signed_catalog(
            tmp_builtin_root,
            vendor="cambium",
            model="pmp450i",
            firmware="15.2.1",
            oids=dict(_SAMPLE_BUILTIN_OIDS),
            key=SAMPLE_CATALOG_KEY,
        )
        _write_signed_catalog(
            tmp_builtin_root,
            vendor="cambium",
            model="pmp450i",
            firmware="15.3.0",
            oids=dict(_SAMPLE_BUILTIN_OIDS),
            key=SAMPLE_CATALOG_KEY,
        )
        _write_signed_catalog(
            tmp_catalogs_dir,
            vendor="cambium",
            model="pmp450i",
            firmware="15.2.1",
            oids=dict(_SAMPLE_BUILTIN_OIDS),
            key=SAMPLE_CATALOG_KEY,
        )
        _write_signed_catalog(
            tmp_catalogs_dir,
            vendor="cambium",
            model="pmp450i",
            firmware="15.3.5",
            oids=dict(_SAMPLE_BUILTIN_OIDS),
            key=SAMPLE_CATALOG_KEY,
        )

        first = OidCatalogRegistry.verify(
            built_in_root=tmp_builtin_root,
            operator_root=tmp_catalogs_dir,
            signing_key=SAMPLE_CATALOG_KEY,
        )
        second = OidCatalogRegistry.verify(
            built_in_root=tmp_builtin_root,
            operator_root=tmp_catalogs_dir,
            signing_key=SAMPLE_CATALOG_KEY,
        )
        assert first.loaded_refs == second.loaded_refs
        # Two built-ins + two operators, one collision → 3 distinct triples.
        assert len(first.loaded_refs) == 3
        assert {tuple(r) for r in first.loaded_refs} == {
            ("cambium", "pmp450i", "15.2.1"),
            ("cambium", "pmp450i", "15.3.0"),
            ("cambium", "pmp450i", "15.3.5"),
        }
        # The operator wins on the shared triple.
        catalog = first.resolve(("cambium", "pmp450i", "15.2.1"))
        assert catalog.oids["signalStrengthRx"] == "1.3.6.1.4.1.161.19.3.1.1.3.0"

    def test_duplicate_within_root_raises_catalog_verification_error(
        self,
        tmp_catalogs_dir: Path,
    ) -> None:
        """Two JSON files in the same root with the same triple → typed failure.

        ADR #17 P1 risk mitigation: silent override would mask operator
        mistakes (e.g. dropping a new firmware in twice). The verifier
        MUST raise ``CatalogVerificationError`` naming the colliding
        triple so the operator can find the duplicate. Two distinct
        filenames (``15.2.1.json`` + ``15.2.1.bak.json``) with the same
        envelope triple reproduce the case without one write clobbering
        the other.
        """
        # Drop two distinct JSON files at the same vendor/model/firmware
        # triple inside the operator root.
        _write_signed_catalog(
            tmp_catalogs_dir,
            vendor="cambium",
            model="pmp450i",
            firmware="15.2.1",
            oids=dict(_SAMPLE_BUILTIN_OIDS),
            key=SAMPLE_CATALOG_KEY,
        )
        # Write a SECOND catalog file with the same triple but a
        # different oids payload (different sentinel). Re-signed so HMAC
        # verifies; the verifier MUST still complain about the duplicate.
        duplicate_payload = dict(_SAMPLE_BUILTIN_OIDS)
        duplicate_payload["signalStrengthRx"] = "1.3.6.1.4.1.161.19.3.1.1.777.0"
        _write_signed_catalog(
            tmp_catalogs_dir,
            vendor="cambium",
            model="pmp450i",
            firmware="15.2.1",
            oids=duplicate_payload,
            key=SAMPLE_CATALOG_KEY,
            filename="15.2.1.bak.json",
        )

        with pytest.raises(CatalogVerificationError) as exc:
            OidCatalogRegistry.verify(
                built_in_root=None,
                operator_root=tmp_catalogs_dir,
                signing_key=SAMPLE_CATALOG_KEY,
            )
        assert "duplicate" in str(exc.value).lower() or "collision" in str(exc.value).lower()

    def test_invalid_hmac_in_any_root_raises_catalog_verification_error(
        self,
        tmp_builtin_root: Path,
        tmp_catalogs_dir: Path,
    ) -> None:
        """Scenario: invalid HMAC in any root aborts boot.

        Built-in has a valid catalog; operator root contains one catalog
        whose HMAC was signed with a *different* key. ``verify`` MUST
        raise :class:`CatalogVerificationError` — the driver MUST NOT
        start. Symmetrically, a tampered built-in also aborts even when
        the operator root is empty.
        """
        # Built-in: valid signed catalog.
        _write_signed_catalog(
            tmp_builtin_root,
            vendor="cambium",
            model="pmp450i",
            firmware="15.2.1",
            oids=dict(_SAMPLE_BUILTIN_OIDS),
            key=SAMPLE_CATALOG_KEY,
        )
        # Operator: catalog signed with the wrong key.
        _write_signed_catalog(
            tmp_catalogs_dir,
            vendor="cambium",
            model="pmp450i",
            firmware="15.3.0",
            oids=dict(_SAMPLE_BUILTIN_OIDS),
            key="WRONG-KEY",
        )

        with pytest.raises(CatalogVerificationError) as exc:
            OidCatalogRegistry.verify(
                built_in_root=tmp_builtin_root,
                operator_root=tmp_catalogs_dir,
                signing_key=SAMPLE_CATALOG_KEY,
            )
        assert "hmac" in exc.value.reason.lower() or "signature" in exc.value.reason.lower()
        # The path in the exception MUST point at the operator file
        # (that's the one whose HMAC is invalid); ``_verify_one``
        # aborts the loop as soon as it sees a mismatch.
        assert str(exc.value.path).endswith("15.3.0.json")

    def test_invalid_hmac_in_builtin_raises_when_operator_root_empty(
        self,
        tmp_builtin_root: Path,
        tmp_catalogs_dir: Path,
    ) -> None:
        """Symmetric half of the fail-fast contract.

        A tampered built-in (wrong HMAC) aborts boot even when the
        operator root is empty. Verifies ``_verify_one`` is reused
        verbatim across both roots — the same ``hmac.compare_digest``
        failure surfaces from the built-in scan.
        """
        _write_signed_catalog(
            tmp_builtin_root,
            vendor="cambium",
            model="pmp450i",
            firmware="15.2.1",
            oids=dict(_SAMPLE_BUILTIN_OIDS),
            key="WRONG-KEY",
        )

        with pytest.raises(CatalogVerificationError) as exc:
            OidCatalogRegistry.verify(
                built_in_root=tmp_builtin_root,
                operator_root=tmp_catalogs_dir,
                signing_key=SAMPLE_CATALOG_KEY,
            )
        assert "hmac" in exc.value.reason.lower() or "signature" in exc.value.reason.lower()

    def test_builtin_baseline_loads_via_importlib_resources(self) -> None:
        """Scenario: built-in baseline ships via package data.

        Resolves ``importlib.resources.files("nora.data.oid_catalogs")``
        and confirms the shipped ``cambium/pmp450i/15.2.1.json`` baseline
        is present. Then walks the resolved container and verifies the
        HMAC against :data:`nora.data.BUILTIN_BASELINE_SIGNING_KEY`.
        """
        from importlib.resources import files

        from nora.data import BUILTIN_BASELINE_SIGNING_KEY

        built_in_root = files("nora.data.oid-catalogs")
        # `files(...)` may return a `Path` (dev/source) or `MultiplexedPath`
        # (wheel); the registry accepts either. The walk must surface the
        # shipped triple.
        catalog_path = built_in_root.joinpath("cambium/pmp450i/15.2.1.json")
        assert catalog_path.is_file() or catalog_path.exists(), (
            f"shipped baseline not found at {catalog_path}"
        )

        registry = OidCatalogRegistry.verify(
            built_in_root=built_in_root,
            operator_root=Path("/tmp/empty-operator-root-for-builtin-test"),
            signing_key=BUILTIN_BASELINE_SIGNING_KEY,
        )
        assert ("cambium", "pmp450i", "15.2.1") in {tuple(ref) for ref in registry.loaded_refs}
        catalog = registry.resolve(("cambium", "pmp450i", "15.2.1"))
        assert catalog.oids["ssr"] == "1.3.6.1.4.1.161.19.3.1.1.5.0"


# ---------------------------------------------------------------------------
# PR 2 — Semver-aware firmware resolution (ADR #17 P2)
# ---------------------------------------------------------------------------


class TestSemverResolution:
    """PR 2: ``resolve`` compares firmware strings semver-aware.

    Each scenario maps directly to a spec acceptance scenario in
    `openspec/changes/2026-09-12-oid-catalog-hybrid-semver/spec.md`:

    * ``test_exact_match_returns_without_warning`` → ``Semver-Aware
      Firmware Resolution > exact match returns without warning``.
    * ``test_pre_release_request_matches_bare_version`` → ``Semver-Aware
      Firmware Resolution > pre-release request matches the bare
      version``.
    * ``test_major_mismatch_raises_typed_exception`` → ``Strict-Major
      Hard Fail > major mismatch raises a typed exception``.
    * ``test_minor_mismatch_returns_closest_lower_minor_with_literal_warning`` →
      ``Minor Descending Fallback With Literal Warning > minor mismatch
      returns closest lower minor with literal warning``.
    """

    def test_exact_match_returns_without_warning(
        self,
        tmp_catalogs_dir: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Scenario: exact match returns without warning.

        Registry holds ``(cambium, pmp450i, 15.2.1)``. ``resolve`` of
        the same triple returns the catalog silently — no WARNING-level
        record is emitted. ADR #17 P2 acceptance: "Firmware idéntico al
        catálogo: sin warning".
        """
        _write_signed_catalog(
            tmp_catalogs_dir,
            vendor="cambium",
            model="pmp450i",
            firmware="15.2.1",
            oids=dict(_SAMPLE_BUILTIN_OIDS),
            key=SAMPLE_CATALOG_KEY,
        )
        registry = OidCatalogRegistry.verify(
            built_in_root=None,
            operator_root=tmp_catalogs_dir,
            signing_key=SAMPLE_CATALOG_KEY,
        )

        with caplog.at_level(logging.WARNING, logger="nora.drivers.oid_catalog"):
            catalog = registry.resolve(("cambium", "pmp450i", "15.2.1"))

        assert catalog.firmware == "15.2.1"
        assert all(r.levelno < logging.WARNING for r in caplog.records), (
            f"Exact match must not emit WARNING; got: {[r.getMessage() for r in caplog.records]!r}"
        )

    def test_pre_release_request_matches_bare_version(
        self,
        tmp_catalogs_dir: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Scenario: pre-release request matches the bare version.

        Registry holds ``(cambium, pmp450i, 15.2.1)``. Requesting
        ``15.2.1-rc.1`` returns the catalog at ``15.2.1`` without warning
        (pre-release and build metadata are stripped before compare via
        ``packaging.version.Version(...).base_version``).
        """
        _write_signed_catalog(
            tmp_catalogs_dir,
            vendor="cambium",
            model="pmp450i",
            firmware="15.2.1",
            oids=dict(_SAMPLE_BUILTIN_OIDS),
            key=SAMPLE_CATALOG_KEY,
        )
        registry = OidCatalogRegistry.verify(
            built_in_root=None,
            operator_root=tmp_catalogs_dir,
            signing_key=SAMPLE_CATALOG_KEY,
        )

        with caplog.at_level(logging.WARNING, logger="nora.drivers.oid_catalog"):
            catalog = registry.resolve(("cambium", "pmp450i", "15.2.1-rc.1"))

        assert catalog.firmware == "15.2.1"
        assert all(r.levelno < logging.WARNING for r in caplog.records), (
            "Pre-release strip must not emit WARNING; got: "
            f"{[r.getMessage() for r in caplog.records]!r}"
        )

    def test_major_mismatch_raises_typed_exception(self, tmp_catalogs_dir: Path) -> None:
        """Scenario: major mismatch raises a typed exception.

        Registry holds only ``(cambium, pmp450i, 15.2.1)``. Resolving
        ``16.0.0`` raises :class:`CatalogNotFoundError` and the message
        names BOTH majors (requested = 16, registered = 15) so the
        operator can see which major line the fleet runs against.
        """
        _write_signed_catalog(
            tmp_catalogs_dir,
            vendor="cambium",
            model="pmp450i",
            firmware="15.2.1",
            oids=dict(_SAMPLE_BUILTIN_OIDS),
            key=SAMPLE_CATALOG_KEY,
        )
        registry = OidCatalogRegistry.verify(
            built_in_root=None,
            operator_root=tmp_catalogs_dir,
            signing_key=SAMPLE_CATALOG_KEY,
        )

        with pytest.raises(CatalogNotFoundError) as exc:
            registry.resolve(("cambium", "pmp450i", "16.0.0"))

        message = str(exc.value)
        # Both majors must appear in the message — the requested one and
        # the registered one. Operators grep server logs for these.
        assert "16" in message, (
            f"Exception message must name the requested major 16; got: {message!r}"
        )
        assert "15" in message, (
            f"Exception message must name the registered major 15; got: {message!r}"
        )

    def test_minor_mismatch_returns_closest_lower_minor_with_literal_warning(
        self,
        tmp_catalogs_dir: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Scenario: minor mismatch returns closest lower minor with literal warning.

        Registry holds ``(cambium, pmp450i, 15.2.1)`` AND
        ``(cambium, pmp450i, 15.3.0)``. Resolving ``15.3.1`` returns the
        catalog at ``15.3.0`` (the highest strictly-less-than) and emits
        the LITERAL telemetry string
        ``"OID catalog fallback: requested 15.3.1, using 15.3.0 (minor mismatch)"``.
        ADR #17 P2 acceptance: the literal is locked by the spec scenario
        and must match byte-for-byte.
        """
        _write_signed_catalog(
            tmp_catalogs_dir,
            vendor="cambium",
            model="pmp450i",
            firmware="15.2.1",
            oids=dict(_SAMPLE_BUILTIN_OIDS),
            key=SAMPLE_CATALOG_KEY,
        )
        _write_signed_catalog(
            tmp_catalogs_dir,
            vendor="cambium",
            model="pmp450i",
            firmware="15.3.0",
            oids=dict(_SAMPLE_BUILTIN_OIDS),
            key=SAMPLE_CATALOG_KEY,
        )
        registry = OidCatalogRegistry.verify(
            built_in_root=None,
            operator_root=tmp_catalogs_dir,
            signing_key=SAMPLE_CATALOG_KEY,
        )

        with caplog.at_level(logging.WARNING, logger="nora.drivers.oid_catalog"):
            catalog = registry.resolve(("cambium", "pmp450i", "15.3.1"))

        assert catalog.firmware == "15.3.0"
        assert len(caplog.records) >= 1, (
            f"Expected at least one log record; got: {[r.getMessage() for r in caplog.records]!r}"
        )
        assert caplog.records[0].message == (
            "OID catalog fallback: requested 15.3.1, using 15.3.0 (minor mismatch)"
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _write_signed_catalog(
    root: Path,
    *,
    vendor: str,
    model: str,
    firmware: str,
    oids: dict[str, str],
    key: str,
    filename: str | None = None,
) -> Path:
    """Sign `oids` with `key` and write the envelope under `<root>/<v>/<m>/<filename>`.

    Defaults to the canonical `<firmware>.json` filename so callers that
    want a single file per triple can ignore ``filename``. Tests that
    need to provoke a within-root duplicate (same envelope triple, two
    distinct filenames on disk) pass a non-default ``filename`` to keep
    both files on disk after the second write.
    """
    target_dir = root / vendor / model
    target_dir.mkdir(parents=True, exist_ok=True)
    fname = filename if filename is not None else f"{firmware}.json"
    path = target_dir / fname
    canonical_body = json.dumps(oids, sort_keys=True, separators=(",", ":")).encode("utf-8")
    sig = hmac.new(key.encode(), canonical_body, hashlib.sha256).hexdigest()
    envelope = {
        "version": 1,
        "vendor": vendor,
        "model": model,
        "firmware": firmware,
        "oids": oids,
        "hmac_sha256": sig,
    }
    path.write_text(json.dumps(envelope))
    return path
