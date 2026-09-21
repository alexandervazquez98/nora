"""Open WebUI declarative sync layer — issue #45 / WU-3.

Pushes versioned NORA system prompts into the Open WebUI chat
front-end so the operator's dashboard sees the same provenance as
embedded callers (``registry.render(name)``). Two profile shapes
per Q7 of the feature plan:

* **Immutable** — ``POST /api/v1/models/create`` with body id
  ``<model_base>-v<X.Y.Z>``. A re-sync of the same NORA version
  re-POSTs the same id; Open WebUI rejects with HTTP 401 and
  ``{"detail": "Model ID already taken"}`` and the orchestrator
  treats this as ``action="already_exists"`` so the audit log
  shows a no-op rather than a failure.
* **Mutable alias** — ``POST /api/v1/models/model/update`` with
  body id ``<model_base>-latest``. The alias always tracks the
  most recent version; rollback is operator-initiated by selecting
  a frozen tag in Open WebUI's model dropdown.

Auth is ``Authorization: Bearer <admin_api_key>`` against the
operator-supplied ``base_url`` (default ``http://localhost:8080``).
The bounded retry budget (``max_retries=2``, ``timeout_seconds=30.0``)
mirrors the post-sweep spectrum XML fetcher
(``nora.drivers.snmp_pmp450i.spectrum_http``) — a single client
lifetime per ``sync_prompts`` call so the connection pool does not
leak across the multi-prompt loop.

Zero-Leakage: the ``base_url`` is the operator's chat front-end
URL — a public-facing host, NOT a private catalog or inventory
URL. The ``model_base`` is the operator's chosen chat-model handle
(e.g. ``nora-netops``); it carries no infrastructure fingerprint.

Verified Open WebUI API surface (sandbox-tested 2026-09-20, PR #76
review comment by alexandervazquez98, and 2026-09-21 post-fix
sandbox re-test):

* **Create** — ``POST /api/v1/models/create`` (NOT ``POST
  /api/v1/models`` which returns HTTP 405).
* **Update** — ``POST /api/v1/models/model/update`` (NOT ``PUT
  /api/v1/models/<id>``) — the route accepts ``id`` in the body
  rather than the URL.
* **Body shape** — ``{id, name, meta: {description, commit_sha,
  release_tag, synced_at, nora_version, prompt_version}, params:
  {system: <rendered-body-with-watermark>}}``. The system prompt
  lives under ``params.system`` (Modelfile convention); top-level
  ``system_prompt`` is silently dropped by the server.
* **Idempotency** — re-syncing an existing model id returns HTTP
  401 with body ``{"detail": "Uh-oh! This model id is already
  registered. Please choose another model id string."}`` (NOT 409
  Conflict, and the substring to match is ``"already registered"``
  — not ``"already taken"`` as the spec text suggested). The
  orchestrator parses the response body via
  ``_is_duplicate_id_response`` and treats the substring match as
  the ``already_exists`` no-op signal; other 401 bodies surface as
  ``OpenWebUIAuthError``. The helper matches BOTH ``"already
  taken"`` and ``"already registered"`` for forward-compat with
  Open WebUI text variations.
* **Alias upsert** — on a fresh deployment where the mutable alias
  ``<model_base>-latest`` does not yet exist, ``POST
  /api/v1/models/model/update`` returns HTTP 404 NOT_FOUND with
  body ``{"detail": "We could not find what you're looking for
  :/"}``. The orchestrator detects this via
  ``_is_alias_not_found_response`` and falls back to a POST on the
  **create** endpoint with the same body, seeding the alias. The
  create-fallback reuses ``_classify_post_status`` so a race where
  the alias appears between the 404 and the retry still classifies
  correctly.
* **Status codes** — 200/201 == success on both POSTs. 401 with
  the duplicate-id detail == ``already_exists``. 401/403 with
  other details == auth error. 404 with the alias-not-found detail
  == trigger for the upsert fallback. Everything else surfaces as
  ``OpenWebUISyncError``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import httpx
from pydantic import BaseModel, ConfigDict, field_validator

if TYPE_CHECKING:
    from nora.prompts.registry import Prompt, PromptRegistry

logger = logging.getLogger("nora.prompts.sync")


# ---------------------------------------------------------------------------
# Config model — frozen Pydantic v2 with trailing-slash normalisation
# ---------------------------------------------------------------------------


class OpenWebUIConfig(BaseModel):
    """Per-invocation Open WebUI target — frozen Pydantic v2 model.

    Frozen so a caller cannot mutate the config mid-sync (would
    silently change the URL or the auth header mid-loop). The
    trailing-slash normaliser keeps URL composition safe — a stray
    ``/`` on ``http://localhost:8080/`` would otherwise yield
    ``http://localhost:8080//api/v1/models`` (double slash) which
    some HTTP routers reject.
    """

    model_config = ConfigDict(frozen=True)

    base_url: str = "http://localhost:8080"
    admin_api_key: str
    model_base: str = "nora-netops"
    timeout_seconds: float = 30.0
    max_retries: int = 2

    @field_validator("base_url")
    @classmethod
    def _strip_trailing_slashes(cls, value: str) -> str:
        """Strip trailing slashes; never emit an empty base URL."""
        return value.rstrip("/") or value

    @property
    def models_url(self) -> str:
        """Absolute URL for the ``/api/v1/models`` collection endpoint."""
        return f"{self.base_url}/api/v1/models"

    @property
    def models_create_url(self) -> str:
        """Absolute URL for the Open WebUI model-create endpoint."""
        return f"{self.base_url}/api/v1/models/create"

    @property
    def models_update_url(self) -> str:
        """Absolute URL for the Open WebUI model-update endpoint.

        Open WebUI's update route accepts the model ``id`` in the
        request body (not the URL path), so this URL has no
        ``<id>`` segment — callers pass the id via the JSON body.
        """
        return f"{self.base_url}/api/v1/models/model/update"


# ---------------------------------------------------------------------------
# Exception hierarchy — typed errors for the Open WebUI REST surface
# ---------------------------------------------------------------------------


class OpenWebUISyncError(Exception):
    """Base error for any Open WebUI sync transport failure."""


class OpenWebUIAuthError(OpenWebUISyncError):
    """401/403 from Open WebUI — operator must fix the API key or RBAC."""


class OpenWebUIConflictError(OpenWebUISyncError):
    """409 from Open WebUI.

    Surfaced as an explicit error class so callers (e.g. the CLI
    script) MAY opt to treat it as success without importing the
    status-code integer — the orchestrator's own idempotency path
    (``action="already_exists"``) is the user-facing surface, and
    this error class is reserved for future ``raise`` call sites
    that want to fail loud on duplicates (none today).
    """


# ---------------------------------------------------------------------------
# Client factory — pure builder, transport injected by tests
# ---------------------------------------------------------------------------


# Status codes treated as success on POST and PUT (covers Open WebUI's
# 200-on-update / 201-on-create split). Everything else fans out to
# one of the typed error classes.
_SUCCESS_STATUS_CODES: Final[frozenset[int]] = frozenset({httpx.codes.OK, httpx.codes.CREATED})
_AUTH_FAILURE_STATUS_CODES: Final[frozenset[int]] = frozenset(
    {httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN}
)


def build_client(config: OpenWebUIConfig) -> httpx.Client:
    """Build a configured ``httpx.Client``.

    Pure factory — no module-level connection pool, no shared
    state. Each ``sync_prompts`` call builds and closes its own
    client so the multi-prompt loop never shares auth headers
    across an invoker's iteration.

    The bearer token is sent on EVERY request via the default
    ``Authorization`` header. Tests override this via
    ``client_factory=`` (e.g. ``httpx.MockTransport``); see the
    test module.
    """
    return httpx.Client(
        base_url=config.base_url,
        timeout=config.timeout_seconds,
        headers={
            "Authorization": f"Bearer {config.admin_api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )


# ---------------------------------------------------------------------------
# Body builder — one prompt at a time
# ---------------------------------------------------------------------------


def _utc_iso_now() -> str:
    """Return the current UTC time in ISO-8601 with explicit ``+00:00`` offset.

    Python's ``datetime.now(UTC).isoformat()`` already returns
    ``+00:00`` so no offset rewrite is needed. The function exists
    so tests can monkey-patch the clock; do not replace with
    ``time.time()`` (epoch float is NOT ISO-8601).
    """
    return datetime.now(tz=UTC).isoformat()


def render_model_profile(
    registry: "PromptRegistry",
    name: str,
    config: OpenWebUIConfig,
    nora_version: str,
    git_sha: str,
    release_tag: str | None,
) -> dict[str, Any]:
    """Build the Open WebUI JSON body for one prompt.

    The body shape (per the verified Open WebUI API surface):

    .. code-block:: json

        {
          "id": "<model_base>-v<X.Y.Z>",
          "name": "<model_base>-v<X.Y.Z>",
          "meta": {
            "description": "<prompt.description>",
            "commit_sha": "<git_sha>",
            "release_tag": "<release_tag or null>",
            "synced_at": "<ISO-8601 UTC>",
            "nora_version": "<nora.__version__>",
            "prompt_version": "<prompt.metadata['version']>"
          },
          "params": {
            "system": "<registry.render(name) — includes watermark>"
          }
        }

    The ``params.system`` is delegated to ``registry.render(name)``
    so the Q6 watermark banner reaches the Open WebUI dashboard
    too — every consumer of the canonical prompt sees the same
    provenance tag. The ``description`` in ``meta`` is the
    front-matter ``description`` of the prompt itself, surfaced
    as the human-readable summary in Open WebUI's model dropdown.

    Raises:
        nora.drivers.exceptions.PromptNotFoundError: when ``name``
            is not in the registry — propagated from
            ``registry.get(name)``. The CLI script catches this and
            exits non-zero with the prompt name in the error.
    """
    prompt = registry.get(name)
    version_str = prompt.metadata.get("version")
    if not isinstance(version_str, str):
        raise OpenWebUISyncError(
            f"prompt {name!r} has no `version` metadata; "
            f"only system prompts may be synced (got metadata={prompt.metadata!r})"
        )
    rendered_body = registry.render(name)
    versioned_id = f"{config.model_base}-v{version_str}"
    return {
        "id": versioned_id,
        "name": versioned_id,
        "meta": {
            "description": prompt.description,
            "commit_sha": git_sha,
            "release_tag": release_tag,
            "synced_at": _utc_iso_now(),
            "nora_version": nora_version,
            "prompt_version": version_str,
        },
        "params": {
            "system": rendered_body,
        },
    }


# ---------------------------------------------------------------------------
# Orchestrator — POST immutable profile + PUT mutable alias
# ---------------------------------------------------------------------------


def _is_syncable_system_prompt(prompt: "Prompt") -> bool:
    """Whether ``prompt`` is a system prompt with version-based metadata.

    Tool-specs (``docs/tool_specs/*.md``) carry ``tier`` /
    ``requires_*`` metadata instead of ``version`` /
    ``checksum_sha256``. Filtering at the orchestrator keeps the
    sync surface narrow — only the version-based system prompts
    are pushed to Open WebUI.
    """
    md = prompt.metadata
    return isinstance(md.get("version"), str) and isinstance(md.get("checksum_sha256"), str)


def _is_duplicate_id_response(status_code: int, body: str) -> bool:
    """Detect Open WebUI's "model id already in use" detail in a 401 response.

    Per the verified API surface (sandbox-tested 2026-09-20 / 2026-09-21),
    re-syncing an existing model id yields HTTP 401 with a
    ``{"detail": "..."}`` body whose message indicates the id is taken.
    The exact substring varies across Open WebUI versions:

    * ``"Model ID already taken"`` (original spec text).
    * ``"Uh-oh! This model id is already registered. Please choose
      another model id string."`` (verified in the Open WebUI source at
      ``constants.py:55`` — ``MODEL_ID_TAKEN`` constant — see PR #76
      post-fix sandbox comment).

    We match EITHER substring so both shapes classify as the
    idempotent ``already_exists`` no-op signal. Returns ``False`` for
    any non-401 status, non-JSON body, or auth-failure detail that
    does not indicate a duplicate id.
    """
    if status_code != httpx.codes.UNAUTHORIZED:
        return False
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return False
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if not isinstance(detail, str):
        return False
    lowered = detail.lower()
    return "already taken" in lowered or "already registered" in lowered


def _is_alias_not_found_response(status_code: int, body: str) -> bool:
    """Detect Open WebUI's "alias does not exist yet" detail in a 404 response.

    Per the verified API surface (sandbox-tested 2026-09-21), a
    first-time sync for the mutable alias (``<model_base>-latest``)
    yields HTTP 404 NOT_FOUND on the update POST with body
    ``{"detail": "We could not find what you're looking for :/"}``.
    The orchestrator's upsert pattern treats this as the signal to
    fall back to the create endpoint with the same body, so the
    alias is seeded on a fresh deployment without operator action.
    """
    if status_code != httpx.codes.NOT_FOUND:
        return False
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return False
    detail = payload.get("detail") if isinstance(payload, dict) else None
    return isinstance(detail, str) and "could not find" in detail.lower()


def _classify_post_status(status_code: int, profile_id: str, body: str) -> str:
    """Classify the POST response into ``created``/``already_exists``/raise.

    Helper for ``sync_prompts`` so the action-name policy lives in
    one place. Per the verified API surface (2026-09-20):
    200/201 → created; 401 with ``{"detail": "Model ID already taken"}``
    → already_exists (idempotent re-run); 401/403 without that detail →
    auth error; anything else → generic transport error.
    """
    if _is_duplicate_id_response(status_code, body):
        return "already_exists"
    if status_code in _SUCCESS_STATUS_CODES:
        return "created"
    if status_code in _AUTH_FAILURE_STATUS_CODES:
        raise OpenWebUIAuthError(
            f"Open WebUI returned HTTP {status_code} on POST {profile_id!r}: {body}"
        )
    raise OpenWebUISyncError(
        f"Open WebUI returned HTTP {status_code} on POST {profile_id!r}: {body}"
    )


def _check_update_status(status_code: int, alias_id: str, body: str) -> None:
    """Validate the POST-to-update-endpoint response — raise on non-success.

    NOTE: does NOT raise on HTTP 404 — the orchestrator inspects the
    body via ``_is_alias_not_found_response`` and falls back to the
    create endpoint for a first-time sync. Any other non-success
    status raises as before.
    """
    if status_code in _SUCCESS_STATUS_CODES:
        return
    if status_code == httpx.codes.NOT_FOUND and _is_alias_not_found_response(status_code, body):
        # Caller handles the upsert fallback; do not raise.
        return
    if status_code in _AUTH_FAILURE_STATUS_CODES:
        raise OpenWebUIAuthError(
            f"Open WebUI returned HTTP {status_code} on POST {alias_id!r} (update): {body}"
        )
    raise OpenWebUISyncError(
        f"Open WebUI returned HTTP {status_code} on POST {alias_id!r} (update): {body}"
    )


def sync_prompts(
    registry: "PromptRegistry",
    config: OpenWebUIConfig,
    nora_version: str,
    git_sha: str,
    release_tag: str | None,
    *,
    client_factory: Callable[[OpenWebUIConfig], httpx.Client] = build_client,
) -> list[dict[str, Any]]:
    """Sync every registered system prompt to Open WebUI.

    For each registered system prompt (filtered by
    ``_is_syncable_system_prompt``):

    1. Build the body via :func:`render_model_profile`.
    2. ``POST /api/v1/models/create`` with body id
       ``<model_base>-v<X.Y.Z>`` — Open WebUI creates an immutable
       versioned profile. 200/201 → ``action="created"``;
       401 with ``{"detail": "Model ID already taken"}`` →
       ``action="already_exists"`` (idempotent re-run);
       401/403/other → typed exception.
    3. Build a sibling body for the mutable alias
       (``<model_base>-latest``) and ``POST
       /api/v1/models/model/update`` (id in body) so the alias
       tracks the latest synced version.

    The update body is the SAME profile body except the ``id`` and
    ``name`` fields are rewritten to the alias — the metadata
    block (description, commit_sha, prompt_version, synced_at) is
    preserved so the alias carries the latest tag's provenance.

    Args:
        registry: a scanned ``PromptRegistry``. Tool-specs are
            silently skipped.
        config: a frozen :class:`OpenWebUIConfig` carrying the
            base URL, admin key, and retry budget.
        nora_version: the NORA version string embedded in metadata
            (typically ``nora.__version__``).
        git_sha: the commit SHA embedded in metadata. The CLI
            script resolves this via ``git rev-parse HEAD`` (or
            ``"unknown"`` in a tarball install).
        release_tag: the tag string (``"v0.3.5"`` or ``None``) — the
            alias of the current release.
        client_factory: an injectable factory (``config ->
            httpx.Client``). Defaults to :func:`build_client`;
            tests override to wire ``httpx.MockTransport``.

    Returns:
        A list of result dicts, one per synced prompt:

        .. code-block:: python

            {
              "name": "<prompt name>",
              "version": "<prompt version>",
              "action": "created" | "already_exists" | "updated",
              "http_status": <int>,
            }

    Raises:
        OpenWebUIAuthError: on 401/403.
        OpenWebUISyncError: on any other non-success status, or
            transport-level httpx errors.
    """
    results: list[dict[str, Any]] = []
    owns_client = client_factory is build_client or callable(client_factory)
    client = client_factory(config)
    try:
        create_url = config.models_create_url
        update_url = config.models_update_url
        for name in registry.names:
            prompt = registry.get(name)
            if not _is_syncable_system_prompt(prompt):
                logger.debug("skipping non-system prompt %r (no version metadata)", name)
                continue

            profile = render_model_profile(
                registry,
                name,
                config,
                nora_version=nora_version,
                git_sha=git_sha,
                release_tag=release_tag,
            )
            profile_id = profile["id"]

            # Step 1 — POST the immutable versioned profile to /api/v1/models/create.
            try:
                post_response = client.post(create_url, json=profile)
            except httpx.HTTPError as exc:
                raise OpenWebUISyncError(
                    f"HTTP transport error during POST {profile_id!r} (create): {exc}"
                ) from exc

            action = _classify_post_status(
                post_response.status_code,
                profile_id,
                post_response.text,
            )

            # Step 2 — POST the mutable alias to /api/v1/models/model/update
            # (id carried in the body, not the URL).
            latest_alias_id = f"{config.model_base}-latest"
            latest_profile = dict(profile)
            latest_profile["id"] = latest_alias_id
            latest_profile["name"] = latest_alias_id

            try:
                update_response = client.post(update_url, json=latest_profile)
            except httpx.HTTPError as exc:
                raise OpenWebUISyncError(
                    f"HTTP transport error during POST {latest_alias_id!r} (update): {exc}"
                ) from exc

            _check_update_status(
                update_response.status_code,
                latest_alias_id,
                update_response.text,
            )

            # Upsert fallback: if the alias does not exist yet (HTTP
            # 404 NOT_FOUND), seed it via the create endpoint with the
            # same body. Subsequent syncs hit the update path normally.
            if _is_alias_not_found_response(update_response.status_code, update_response.text):
                logger.debug("alias %r does not exist yet; falling back to create", latest_alias_id)
                try:
                    create_alias_response = client.post(create_url, json=latest_profile)
                except httpx.HTTPError as exc:
                    raise OpenWebUISyncError(
                        f"HTTP transport error during POST {latest_alias_id!r} "
                        f"(alias create fallback): {exc}"
                    ) from exc
                # The create fallback reuses the same classifier — if
                # the alias raced into existence between the 404 and
                # our retry, the duplicate-id detail yields
                # action="already_exists" (no-op, correct outcome).
                _classify_post_status(
                    create_alias_response.status_code,
                    latest_alias_id,
                    create_alias_response.text,
                )

            results.append(
                {
                    "name": name,
                    "version": prompt.metadata["version"],
                    "action": action,
                    "http_status": post_response.status_code,
                }
            )
    finally:
        # We always built the client (tests inject a factory that
        # returns a one-shot ``httpx.Client`` too). Closing is
        # idempotent on a closed client and keeps the connection
        # pool from leaking across the multi-prompt loop.
        try:
            client.close()
        except Exception:  # pragma: no cover -- close is best-effort
            pass
    # Silence the unused-variable lint for `owns_client` -- the marker
    # is kept for future readers who may want to elide the close()
    # in a debug-build path. Currently the close() is unconditional.
    _ = owns_client
    return results


__all__ = [
    "OpenWebUIConfig",
    "OpenWebUISyncError",
    "OpenWebUIAuthError",
    "OpenWebUIConflictError",
    "build_client",
    "render_model_profile",
    "sync_prompts",
]
