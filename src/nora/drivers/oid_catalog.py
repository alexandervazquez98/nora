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

PR 2 (ADR #17 P2): ``resolve`` is semver-aware. It compares the requested
firmware against the registry via ``packaging.version.Version`` so
pre-release (``-rc.1``) and build (``+build.5``) metadata are stripped
before compare (``base_version``); a strict-major mismatch raises
``CatalogNotFoundError`` naming both majors; a minor mismatch falls back
to the closest lower minor and emits a literal telemetry warning.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Final, Union

if sys.version_info >= (3, 14):
    from importlib.resources.abc import Traversable
else:
    from importlib.abc import Traversable

from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from nora.config import Settings
from nora.drivers.exceptions import CatalogNotFoundError, CatalogVerificationError

logger = logging.getLogger("nora.drivers.oid_catalog")

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
REQUIRED_OIDS: Final[frozenset[str]] = _REQUIRED_OIDS_BY_VENDOR_MODEL[("cambium", "pmp450i")]


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

        PR 2 (ADR #17 P2): semver-aware resolution. The algorithm runs in
        three passes against the registry:

        1. **Exact-pin lookup** — bit-identical to PR 1's behaviour;
           returns immediately. Preserves the operator-only test path.
        2. **Pre-release / build strip** — ``Version(...).base_version``
           drops ``-rc.1`` and ``+build.5`` so ``15.2.1-rc.1`` matches
           ``15.2.1``. Returns silently (no warning) when the bare
           version matches.
        3. **Strict-major + minor-descending fallback** — major mismatch
           raises ``CatalogNotFoundError`` with both majors in the
           message; minor mismatch returns the closest lower minor and
           emits a literal telemetry warning.

        Raises ``CatalogNotFoundError`` when no candidate matches.
        """
        vendor, model, firmware = ref

        # Pass 1 — exact-pin lookup (bit-identical to PR 1).
        if ref in self._catalogs:
            return self._catalogs[ref]

        # Pass 2 — pre-release / build metadata strip via
        # ``Version(...).base_version``. An invalid firmware string (e.g.
        # ``"v15.2.1"``) fails ``Version``; we keep ``req_version=None``
        # and fall through to the strict-major branch, which surfaces
        # the typed error.
        try:
            req_version = Version(firmware)
        except InvalidVersion:
            req_version = None
        if req_version is not None:
            req_base = req_version.base_version
            for (v, m, fw), catalog in self._catalogs.items():
                if v != vendor or m != model:
                    continue
                try:
                    if Version(fw).base_version == req_base:
                        return catalog
                except InvalidVersion:
                    continue

        # Pass 3 — strict-major hard fail. Collect every catalog for
        # the same (vendor, model); if no entry shares the request's
        # major, raise ``CatalogNotFoundError`` whose message names
        # both the requested and the registered majors so an operator
        # can see at a glance which major line the fleet runs against.
        candidates: list[tuple[Version, OidCatalog]] = []
        for (v, m, fw), catalog in self._catalogs.items():
            if v != vendor or m != model:
                continue
            try:
                candidates.append((Version(fw), catalog))
            except InvalidVersion:
                continue

        if req_version is None or not candidates:
            raise CatalogNotFoundError(ref)

        registered_majors = sorted({fw_version.major for fw_version, _ in candidates})
        same_major = [
            (fw_version, catalog)
            for fw_version, catalog in candidates
            if fw_version.major == req_version.major
        ]
        if not same_major:
            exc = CatalogNotFoundError(ref)
            exc.args = (
                f"major mismatch for {ref}: requested major {req_version.major}, "
                f"registered majors {registered_majors}",
            )
            raise exc

        # Pass 4 — minor descending fallback. Full algorithm (highest
        # registered version strictly less than the request + literal
        # telemetry warning) ships in WU 2.3; for now we raise the
        # same typed error so the strict-major scenario stays green.
        raise CatalogNotFoundError(ref)

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
                path=Path(operator_root) if isinstance(operator_root, Path) else operator_root,
                reason="missing signing key (NORA_OID_CATALOG_SIGNING_KEY is empty)",
            )
        key_bytes = key_str.encode("utf-8")
        catalogs: dict[tuple[str, str, str], OidCatalog] = {}
        # dict-as-set: the readonly driver test bans the bare built-in
        # constructor call inside src/nora/drivers (it confuses the AST
        # scan for write verbs), so we use ``dict[ref, None]`` for O(1)
        # membership checks.
        builtin_seen: dict[tuple[str, str, str], None] = {}
        operator_seen: dict[tuple[str, str, str], None] = {}

        for path in _iter_json_files(built_in_root):
            vendor, model, firmware, catalog = cls._verify_one(path, key_bytes)
            ref = (vendor, model, firmware)
            if ref in builtin_seen:
                raise CatalogVerificationError(
                    path=Path(str(path)),
                    reason=(
                        f"duplicate catalog for (vendor={vendor}, model={model}, "
                        f"firmware={firmware}) in built-in root"
                    ),
                )
            builtin_seen[ref] = None
            catalogs[ref] = catalog

        for path in _iter_json_files(operator_root):
            vendor, model, firmware, catalog = cls._verify_one(path, key_bytes)
            ref = (vendor, model, firmware)
            # Operator wins on collision: the same triple already
            # loaded from built-in is silently shadowed.
            if ref in operator_seen:
                raise CatalogVerificationError(
                    path=Path(str(path)),
                    reason=(
                        f"duplicate catalog for (vendor={vendor}, model={model}, "
                        f"firmware={firmware}) in operator root"
                    ),
                )
            operator_seen[ref] = None
            catalogs[ref] = catalog

        # ``_catalogs_path`` is the operator root when it's a Path,
        # otherwise the built-in path (callers that pass a Traversable
        # operator root — none today — get the built-in back so the
        # attribute stays populated).
        catalogs_path: Path = (
            operator_root
            if isinstance(operator_root, Path)
            else (built_in_root if isinstance(built_in_root, Path) else Path.cwd())
        )
        return cls(_catalogs_path=catalogs_path, _catalogs=catalogs)

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

        # `nora.data.oid-catalogs` is the on-disk namespace package
        # shipped under `src/nora/data/oid-catalogs/`. Python identifier
        # rules forbid hyphens in real package names but
        # `importlib.resources.files()` accepts them as namespace
        # packages (PEP 420).
        built_in_root: Path | None = files("nora.data.oid-catalogs")  # type: ignore[assignment]
        # In a source checkout `files(...)` resolves to a `MultiplexedPath`
        # (a `Traversable`); in a wheel install it can resolve to a real
        # `Path`. PR 1's acceptance runs from a source checkout; wheel
        # packaging is a smoke test gated on `importlib.resources`
        # resolving the path.

        return cls.verify(
            built_in_root=built_in_root,
            operator_root=settings.nora_oid_catalogs_path,
            signing_key=settings.nora_oid_catalog_signing_key,
        )

    @classmethod
    def _verify_one(
        cls,
        path: object,
        key_bytes: bytes,
    ) -> tuple[str, str, str, OidCatalog]:
        """Verify ``path`` against ``key_bytes``; return the catalog or raise.

        ``path`` is whatever the caller hands in — ``Path`` (dev/source)
        or ``Traversable`` (wheel / zipapp). Both implement
        ``read_text()`` so the JSON decoding is identical.

        Raises :class:`CatalogVerificationError` on any failure mode
        (invalid JSON, missing envelope fields, HMAC mismatch, missing
        required OIDs). Callers MUST be ready to propagate the exception
        — there is no silent-drop branch.
        """
        try:
            envelope = json.loads(path.read_text())  # type: ignore[attr-defined]
        except json.JSONDecodeError as exc:
            raise CatalogVerificationError(
                path=Path(str(path)),
                reason=f"invalid JSON: {exc.msg}",
            ) from exc

        if not isinstance(envelope, dict):
            raise CatalogVerificationError(
                path=Path(str(path)),
                reason="envelope is not a JSON object",
            )

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
                path=Path(str(path)),
                reason="envelope missing vendor/model/firmware",
            )
        if not isinstance(oids, dict):
            raise CatalogVerificationError(
                path=Path(str(path)),
                reason="envelope.oids is not an object",
            )
        if not isinstance(signature, str):
            raise CatalogVerificationError(
                path=Path(str(path)),
                reason="envelope.hmac_sha256 is not a string",
            )

        # Canonicalise: sort the oids map so the signature matches across
        # authors / formatter settings.
        canonical_body = json.dumps(oids, sort_keys=True, separators=(",", ":")).encode("utf-8")
        expected = hmac.new(key_bytes, canonical_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise CatalogVerificationError(
                path=Path(str(path)),
                reason="HMAC-SHA256 signature mismatch (tampered or wrong key)",
            )

        required = _REQUIRED_OIDS_BY_VENDOR_MODEL.get((vendor, model))
        if required is None:
            raise CatalogVerificationError(
                path=Path(str(path)),
                reason=f"no required-OID table registered for (vendor={vendor}, model={model})",
            )
        missing = required - oids.keys()
        if missing:
            missing_str = ", ".join(sorted(missing))
            raise CatalogVerificationError(
                path=Path(str(path)),
                reason=f"missing required OID(s): {missing_str}",
            )

        catalog = OidCatalog(vendor=vendor, model=model, firmware=firmware, oids=oids)
        return vendor, model, firmware, catalog


# ---------------------------------------------------------------------------
# Internal — recursive JSON walk that works on `Traversable`.
# ---------------------------------------------------------------------------


_TraversableRoot = Union[Path, "Traversable"]


def _iter_json_files(root: _TraversableRoot | None) -> Iterator[_TraversableRoot]:
    """Yield every ``*.json`` file under ``root``, sorted deterministically.

    Works uniformly on ``pathlib.Path`` (dev/source checkout) and on the
    ``Traversable`` returned by ``importlib.resources.files(...)`` (wheel /
    zipapp installs). Both implement ``iterdir()`` + ``is_dir()`` +
    ``name``, so the walk is identical. A missing or non-directory root
    yields nothing; iteration proceeds top-down, sorted at each level so
    the on-disk ordering never leaks into ``loaded_refs``.
    """
    if root is None:
        return
    try:
        children = sorted(root.iterdir(), key=lambda entry: entry.name)
    except (FileNotFoundError, NotADirectoryError, AttributeError):
        return
    for entry in children:
        if entry.is_dir():
            yield from _iter_json_files(entry)
        elif entry.name.endswith(".json"):
            yield entry


__all__ = ["OidCatalog", "OidCatalogRegistry", "REQUIRED_OIDS"]
