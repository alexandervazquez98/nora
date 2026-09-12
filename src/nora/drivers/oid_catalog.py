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

PR 1 (ADR #17): catalogs ship in TWO roots — a built-in baseline reachable
via ``importlib.resources.files("nora.data.oid_catalogs")`` and the
operator override at ``Settings.nora_oid_catalogs_path``. On a
``(vendor, model, firmware)`` collision the operator copy wins. The
required-OID set is now per-``(vendor, model)`` so a second vendor
can introduce its own schema without a global rename.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from nora.config import Settings
from nora.drivers.exceptions import CatalogNotFoundError, CatalogVerificationError

# ---------------------------------------------------------------------------
# REQUIRED_OIDS — per-(vendor, model) required-OID table.
#
# ADR #17 P0: the schema set is scoped per triple so a second vendor
# ships its own entry without a global rename. The driver import
# (`REQUIRED_OIDS`) keeps pointing at the PMP 450i triple; #14 can
# replace it with a `Device`-scoped lookup without re-issuing this
# module's surface.
# ---------------------------------------------------------------------------

_REQUIRED_OIDS_BY_VENDOR_MODEL: Final[dict[tuple[str, str], frozenset[str]]] = {
    ("cambium", "pmp450i"): frozenset(
        {
            "radioDownlinkRate",
            "radioUplinkRate",
            "signalStrengthRx",
            "signalStrengthTx",
            "ssr",
            "modulationMode",
        }
    ),
}

# Derived alias — what the existing driver import and the R5 test assert
# against. New code SHOULD use `_REQUIRED_OIDS_BY_VENDOR_MODEL[...]` so a
# second vendor is additive.
REQUIRED_OIDS: Final[frozenset[str]] = _REQUIRED_OIDS_BY_VENDOR_MODEL[
    ("cambium", "pmp450i")
]


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
    def verify(
        cls,
        *,
        built_in_root: Path | None,
        operator_root: Path,
        signing_key: SecretStr | str | None,
    ) -> "OidCatalogRegistry":
        """HMAC-verify every catalog file across two roots.

        PR 1 (ADR #17): the registry is loaded from a built-in baseline
        (shipped via ``importlib.resources.files("nora.data.oid_catalogs")``
        in production) and the operator override at
        ``Settings.nora_oid_catalogs_path``. On a ``(vendor, model,
        firmware)`` collision the operator copy wins — the built-in is
        shadowed, not deleted. Both roots are scanned deterministically
        (sorted at every level) so filesystem ordering never leaks into
        ``loaded_refs``.

        ``signing_key`` accepts either a :class:`SecretStr` (production,
        from :class:`Settings`) or a plain ``str`` (hermetic tests).
        Empty / ``None`` → :class:`CatalogVerificationError`.

        Order of operations:

        1. Resolve the signing key.
        2. Walk the built-in root (if any) first; verify every JSON file.
        3. Walk the operator root (if it exists); verify every JSON file,
           shadowing any built-in copy of the same triple.
        4. Build the registry; ``operator_root`` is the canonical path on
           the returned instance (mirrors pre-PR1 behaviour).
        """
        key_str = cls._coerce_signing_key(signing_key)
        if key_str is None or key_str == "":
            raise CatalogVerificationError(
                path=operator_root,
                reason="missing signing key (NORA_OID_CATALOG_SIGNING_KEY is empty)",
            )
        key_bytes = key_str.encode("utf-8")
        catalogs: dict[tuple[str, str, str], OidCatalog] = {}

        if built_in_root is not None and built_in_root.exists():
            for path in sorted(built_in_root.rglob("*.json")):
                vendor, model, firmware, catalog = cls._verify_one(path, key_bytes)
                catalogs[(vendor, model, firmware)] = catalog

        if operator_root.exists():
            for path in sorted(operator_root.rglob("*.json")):
                vendor, model, firmware, catalog = cls._verify_one(path, key_bytes)
                catalogs[(vendor, model, firmware)] = catalog

        return cls(_catalogs_path=operator_root, _catalogs=catalogs)

    @staticmethod
    def _coerce_signing_key(signing_key: SecretStr | str | None) -> str | None:
        """Coerce ``signing_key`` to a plain ``str`` (or ``None``).

        Lets :meth:`verify` accept either a :class:`SecretStr` (production)
        or a ``str`` (hermetic tests) without forcing every test fixture
        through ``SecretStr(...)``.
        """
        if signing_key is None:
            return None
        if isinstance(signing_key, SecretStr):
            return signing_key.get_secret_value()
        return str(signing_key)

    @classmethod
    def verify_all(cls, settings: Settings) -> "OidCatalogRegistry":
        """Thin wrapper: load the built-in baseline plus the operator root.

        Kept for backwards compatibility with the boot wiring in #17 P0.
        Resolves the built-in via ``importlib.resources.files`` so the
        shipped ``src/nora/data/oid-catalogs/`` baseline ships in any
        install layout (source tree, wheel, zipapp). The two-root
        precedence — operator overwrites built-in on collision — lives
        in :meth:`verify`.
        """
        from importlib.resources import files

        built_in_root: Path | None = files("nora.data.oid_catalogs")  # type: ignore[assignment]
        # In a source checkout `files(...)` resolves to a real `Path`; in a
        # wheel install it can resolve to a `Traversable`. PR 1's
        # acceptance runs from a source checkout; wheel packaging is a
        # smoke test gated on `importlib.resources` resolving the path.

        return cls.verify(
            built_in_root=built_in_root,
            operator_root=settings.nora_oid_catalogs_path,
            signing_key=settings.nora_oid_catalog_signing_key,
        )

    @classmethod
    def _verify_one(cls, path: Path, key_bytes: bytes) -> tuple[str, str, str, OidCatalog]:
        """Verify `path` against `key_bytes`; return the catalog or raise.

        Raises :class:`CatalogVerificationError` on any failure mode
        (invalid JSON, missing envelope fields, HMAC mismatch, missing
        required OIDs). Callers MUST be ready to propagate the exception
        — there is no silent-drop branch.
        """
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

        required = _REQUIRED_OIDS_BY_VENDOR_MODEL.get((vendor, model))
        if required is None:
            raise CatalogVerificationError(
                path=path,
                reason=f"no required-OID table registered for (vendor={vendor}, model={model})",
            )
        missing = required - oids.keys()
        if missing:
            missing_str = ", ".join(sorted(missing))
            raise CatalogVerificationError(
                path=path, reason=f"missing required OID(s): {missing_str}"
            )

        catalog = OidCatalog(vendor=vendor, model=model, firmware=firmware, oids=oids)
        return vendor, model, firmware, catalog


__all__ = ["OidCatalog", "OidCatalogRegistry", "REQUIRED_OIDS"]
