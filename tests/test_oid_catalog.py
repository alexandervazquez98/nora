"""Tests for the OID catalog loader + HMAC verifier.

Maps OidCatalog-R1..R7 scenarios from
`openspec/changes/phase2-pmp450i-driver/specs/oid-catalog/spec.md`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
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
# R2 — Per-firmware pin (fail-closed)
# ---------------------------------------------------------------------------


def test_unknown_firmware_raises_catalog_not_found(tmp_catalogs_dir: Path) -> None:
    """No matching catalog file → `CatalogNotFoundError`, driver does not start."""
    registry = OidCatalogRegistry.verify(
        built_in_root=None,
        operator_root=tmp_catalogs_dir,
        signing_key="any-key",
    )
    with pytest.raises(CatalogNotFoundError) as exc:
        registry.resolve(("cambium", "pmp450i", "99.0.0"))
    assert "99.0.0" in str(exc.value)
    assert "cambium" in str(exc.value)


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

    def test_required_oids_alias_matches_pmp450i_table(self) -> None:
        """`REQUIRED_OIDS` is the derived alias for the PMP 450i triple.

        Driver import (`from nora.drivers.oid_catalog import REQUIRED_OIDS`)
        stays untouched; the alias is what the existing R5 test asserts
        against. PR 1 swaps the source from a hand-written frozenset to
        `_REQUIRED_OIDS_BY_VENDOR_MODEL[("cambium", "pmp450i")]`.
        """
        assert REQUIRED_OIDS is _REQUIRED_OIDS_BY_VENDOR_MODEL[("cambium", "pmp450i")]

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
        canonical_body = json.dumps(
            payload["oids"], sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
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
# helpers
# ---------------------------------------------------------------------------
