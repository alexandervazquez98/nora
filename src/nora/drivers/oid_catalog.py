"""OID catalog loader + HMAC-SHA256 verifier.

Contract (OidCatalog-R1..R7):

* Catalogs live under ``<oid_catalogs_path>/<vendor>/<model>/<firmware>.json``.
* Each catalog is JSON: ``{version, vendor, model, firmware, oids, hmac_sha256}``.
* The `oids` map is stable public object name (e.g. ``radioDownlinkRate``)
  to dotted OID. No MIB prose anywhere.
* HMAC-SHA256 verification happens once at boot in
  ``OidCatalogRegistry.verify_all``; tampering or rotation failure surfaces
  ``CatalogVerificationError``.
* Runtime fuzzy matching is forbidden — an unknown firmware pin raises
  ``CatalogNotFoundError`` and the driver does not start.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from nora.config import Settings
from nora.drivers.exceptions import CatalogNotFoundError, CatalogVerificationError

# ---------------------------------------------------------------------------
# REQUIRED_OIDS — the set of stable public object names the driver
# insists every catalog MUST expose. Missing entries fail schema
# validation at boot (OidCatalog-R5).
# ---------------------------------------------------------------------------

REQUIRED_OIDS: Final[frozenset[str]] = frozenset(
    {
        "radioDownlinkRate",
        "radioUplinkRate",
        "signalStrengthRx",
        "signalStrengthTx",
        "ssr",
        "modulationMode",
    }
)


class OidCatalog(BaseModel):
    """A single verified catalog for one (vendor, model, firmware) triple."""

    model_config = ConfigDict(frozen=True)

    vendor: str
    model: str
    firmware: str
    oids: dict[str, str] = Field(default_factory=dict)


class OidCatalogRegistry:
    """Immutable registry of verified catalogs, loaded once at boot.

    `verify_all` is the single entry point: it scans the catalogs path,
    HMAC-verifies every JSON file, validates the schema, and returns a
    registry that downstream code can `resolve(...)` against.
    """

    def __init__(
        self,
        *,
        _catalogs_path: Path,
        _catalogs: dict[tuple[str, str, str], OidCatalog],
    ) -> None:
        self._catalogs_path = _catalogs_path
        self._catalogs = _catalogs

    @property
    def catalogs_path(self) -> Path:
        return self._catalogs_path

    @property
    def loaded_refs(self) -> list[tuple[str, str, str]]:
        """Return every (vendor, model, firmware) triple that loaded."""
        return sorted(self._catalogs.keys())

    def resolve(self, ref: tuple[str, str, str]) -> OidCatalog:
        """Return the verified catalog for `(vendor, model, firmware)`.

        Raises `CatalogNotFoundError` if the registry has no entry for
        the requested triple.
        """
        try:
            return self._catalogs[ref]
        except KeyError as exc:
            vendor, model, firmware = ref
            raise CatalogNotFoundError(ref) from exc

    # ------------------------------------------------------------------
    # Boot-time construction
    # ------------------------------------------------------------------

    @classmethod
    def verify_all(cls, settings: Settings) -> "OidCatalogRegistry":
        """HMAC-verify every catalog file under `settings.nora_oid_catalogs_path`.

        Order of operations:

        1. Resolve the signing key. Empty / None → `CatalogVerificationError`.
        2. Walk the tree. Every JSON file under the root becomes a candidate.
        3. Decode the envelope, recompute HMAC-SHA256 over the canonicalised
           `oids` map, and compare with `hmac.compare_digest`.
        4. Validate `REQUIRED_OIDS ⊆ catalog.oids`. A missing required OID
           is a verification failure.
        5. Build the registry.
        """
        signing_key = settings.nora_oid_catalog_signing_key
        if signing_key is None or signing_key.get_secret_value() == "":
            raise CatalogVerificationError(
                path=settings.nora_oid_catalogs_path,
                reason="missing signing key (NORA_OID_CATALOG_SIGNING_KEY is empty)",
            )
        key_bytes = signing_key.get_secret_value().encode("utf-8")
        catalogs: dict[tuple[str, str, str], OidCatalog] = {}
        root = settings.nora_oid_catalogs_path
        if not root.exists():
            # No catalogs directory is treated as an empty registry; the
            # first device that needs a catalog will raise CatalogNotFound.
            return cls(_catalogs_path=root, _catalogs=catalogs)

        for path in sorted(root.rglob("*.json")):
            ref = cls._verify_one(path, key_bytes)
            if ref is None:
                continue
            vendor, model, firmware, catalog = ref
            catalogs[(vendor, model, firmware)] = catalog

        return cls(_catalogs_path=root, _catalogs=catalogs)

    @classmethod
    def _verify_one(cls, path: Path, key_bytes: bytes) -> tuple[str, str, str, OidCatalog] | None:
        """Verify `path` against `key_bytes`; return the catalog or raise."""
        try:
            envelope = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise CatalogVerificationError(path=path, reason=f"invalid JSON: {exc.msg}") from exc

        if not isinstance(envelope, dict):
            raise CatalogVerificationError(path=path, reason="envelope is not a JSON object")

        vendor = envelope.get("vendor")
        model = envelope.get("model")
        firmware = envelope.get("firmware")
        oids = envelope.get("oids")
        signature = envelope.get("hmac_sha256")
        if (
            not isinstance(vendor, str)
            or not isinstance(model, str)
            or not isinstance(firmware, str)
        ):
            raise CatalogVerificationError(
                path=path, reason="envelope missing vendor/model/firmware"
            )
        if not isinstance(oids, dict):
            raise CatalogVerificationError(path=path, reason="envelope.oids is not an object")
        if not isinstance(signature, str):
            raise CatalogVerificationError(path=path, reason="envelope.hmac_sha256 is not a string")

        # Canonicalise: sort the oids map so the signature matches across
        # authors / formatter settings.
        canonical_body = json.dumps(oids, sort_keys=True, separators=(",", ":")).encode("utf-8")
        expected = hmac.new(key_bytes, canonical_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise CatalogVerificationError(
                path=path, reason="HMAC-SHA256 signature mismatch (tampered or wrong key)"
            )

        missing = REQUIRED_OIDS - oids.keys()
        if missing:
            missing_str = ", ".join(sorted(missing))
            raise CatalogVerificationError(
                path=path, reason=f"missing required OID(s): {missing_str}"
            )

        catalog = OidCatalog(vendor=vendor, model=model, firmware=firmware, oids=oids)
        return vendor, model, firmware, catalog


__all__ = ["OidCatalog", "OidCatalogRegistry", "REQUIRED_OIDS"]
