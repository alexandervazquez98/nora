"""Built-in OID catalog baseline shipped with the package.

PR 1 (ADR #17): the registry loads catalogs from two roots. The
operator root lives at ``Settings.nora_oid_catalogs_path`` and is
operator-managed. The *built-in* root is the directory next to this
``__init__.py`` — it's reached via
``importlib.resources.files("nora.data.oid_catalogs")`` so it ships
in every install layout (source checkout, wheel, zipapp).

The shipped ``cambium/pmp450i/15.2.1.json`` is the v1 baseline. It's
HMAC-SHA256-signed with :data:`BUILTIN_BASELINE_SIGNING_KEY` — a
**placeholder** key so the registry can load it during local
development without leaking a production secret into the repo.
Operators MUST rotate this key for production: re-sign the baseline
via ``scripts/sign_catalog.py --key "$NORA_OID_CATALOG_SIGNING_KEY"``
and ship the result alongside a rotated key.

On boot the operator's key (from
``Settings.nora_oid_catalog_signing_key``) MUST match the key the
built-in baseline was signed with; otherwise ``verify_all`` raises
:class:`CatalogVerificationError` and the driver does not start.
Override precedence still works — operators who want a different
baseline just drop their own catalog under
``$NORA_OID_CATALOGS_PATH/cambium/pmp450i/`` and the operator copy
shadows the built-in.
"""

from __future__ import annotations

# Placeholder HMAC key for the shipped v1 built-in baseline. NEVER use
# this in production — see the module docstring for the rotation
# workflow. Kept here (instead of in `tests/conftest.py`) so the
# registry can verify the baseline in dev without forcing the test
# suite to set up a separate fixture.
BUILTIN_BASELINE_SIGNING_KEY: str = "nora-built-in-baseline-placeholder-key-do-not-use-in-prod"

__all__ = ["BUILTIN_BASELINE_SIGNING_KEY"]
