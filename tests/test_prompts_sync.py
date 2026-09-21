"""Tests for the Open WebUI declarative sync layer — issue #45 / WU-3.

Maps the prompt-versioning WU-3 contract:

* ``OpenWebUIConfig`` — frozen Pydantic model that strips trailing ``/``
  from ``base_url`` and carries the operator-overridable defaults
  (``http://localhost:8080``, ``nora-netops``, 30s, 2 retries).
* ``render_model_profile`` — builds the JSON body for one prompt:
  ``{id, name, meta, params: {system: <rendered>}}`` plus the Q7
  metadata block (``description``, ``commit_sha``, ``release_tag``,
  ``synced_at``, ``nora_version``, ``prompt_version``). Pulls the
  body via ``registry.render(name)`` so the watermark banner is
  included.
* ``sync_prompts`` — orchestrates a POST to ``/api/v1/models/create``
  for the immutable ``<base>-v<X.Y.Z>`` profile and a POST to
  ``/api/v1/models/model/update`` (id in body) for the mutable
  ``<base>-latest`` alias. Returns one result dict per prompt with
  ``action in {"created", "already_exists", "updated"}``.
* Exception contract — typed errors on 401/403/5xx; a 401 with body
  ``{"detail": "Model ID already taken"}`` is treated as the
  idempotent ``already_exists`` signal (verified API surface per
  PR #76 sandbox test).

Zero-Leakage: ``http://testnet`` / ``192.0.2.x`` style addresses only.
Hermeticity: ``httpx.MockTransport`` wired into ``build_client`` via
the optional ``client_factory`` injection point — no real network,
no ``.env`` mutation, no console scripts.

The Open WebUI REST surface used here (``POST /api/v1/models/create``
for new model creation and ``POST /api/v1/models/model/update`` for
mutating an existing one with id-in-body) is the verified behaviour
of Open WebUI's ``models.create`` / ``models.update`` endpoints per
the sandbox validation captured in PR #76's review thread.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
import pytest

from nora.prompts.registry import Prompt, PromptRegistry

# ---------------------------------------------------------------------------
# Helpers — hermetic PromptRegistry + mock HTTP client
# ---------------------------------------------------------------------------


def _make_registry(
    prompts: list[tuple[str, str, str]],
    *,
    include_checksum: bool = True,
) -> PromptRegistry:
    """Build a ``PromptRegistry`` from in-memory prompts (no file I/O).

    Args:
        prompts: list of ``(name, version, body)`` tuples.
        include_checksum: when True (default), populate the
            ``checksum_sha256`` metadata field with a 64-char zero
            hex so ``render()`` produces the watermark banner. When
            False, the metadata only carries ``version`` —
            ``render()`` then returns the bare body (the
            tool-spec-style fallback).

    Uses the package-internal constructor ``_prompts=`` so the test
    does NOT need a real ``.md`` file on disk. The registry's scan-
    time validators (front-matter schema, checksum match, nora
    compatibility range) are bypassed for this test surface — the
    registry's RENDER contract is what we exercise here, not the
    scanner's. Any future refactor of ``render()`` must keep this
    helper working.
    """
    metadata: dict[str, Any] = {"version": "", "checksum_sha256": ""}
    # Placeholders filled per-prompt below.
    prompt_dict: dict[str, Prompt] = {}
    for name, version, body in prompts:
        per_metadata: dict[str, Any] = {"version": version}
        if include_checksum:
            per_metadata["checksum_sha256"] = "0" * 64
            per_metadata["nora_compatibility"] = ">=0.3.4,<0.4.0"
            per_metadata["governance"] = {"tier_0": 0, "tier_1": 0, "tier_2": 0}
        prompt_dict[name] = Prompt(
            name=name,
            description=f"Test prompt {name}",
            body=body,
            metadata=per_metadata,
        )
    # Silence the unused-variable lint — kept for clarity at the
    # call site when future contributors want to vary the metadata.
    _ = metadata
    return PromptRegistry(_prompts=prompt_dict, _source_dirs=())


def _make_mock_client(handler) -> httpx.Client:
    """Build a one-shot httpx.Client wired to ``httpx.MockTransport``."""
    return httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0)


# ---------------------------------------------------------------------------
# OpenWebUIConfig — defaults + trailing-slash normalisation
# ---------------------------------------------------------------------------


def test_config_defaults() -> None:
    """Instantiating ``OpenWebUIConfig`` with no args yields the documented defaults.

    The defaults pin Q7 naming (``model_base="nora-netops"``), the
    Open WebUI base URL (``http://localhost:8080``), and the bounded
    retry budget (2 retries, 30s timeout).
    """
    from nora.prompts.sync import OpenWebUIConfig

    config = OpenWebUIConfig(admin_api_key="dummy")
    assert config.base_url == "http://localhost:8080"
    assert config.model_base == "nora-netops"
    assert config.timeout_seconds == 30.0
    assert config.max_retries == 2


def test_config_strips_trailing_slash_from_base_url() -> None:
    """A trailing ``/`` on ``base_url`` is normalised so URL concatenation is safe.

    Without stripping, ``{base_url}/api/v1/models`` would yield
    ``http://x//api/v1/models`` (double slash) which some HTTP
    routers reject. The model validator runs once at construction.
    """
    from nora.prompts.sync import OpenWebUIConfig

    config = OpenWebUIConfig(admin_api_key="dummy", base_url="http://localhost:9000/")
    assert config.base_url == "http://localhost:9000"
    # Multi-slash also collapses to a single trailing-slash-free form.
    config_multi = OpenWebUIConfig(admin_api_key="dummy", base_url="http://x///")
    assert config_multi.base_url == "http://x"


def test_config_is_frozen() -> None:
    """``OpenWebUIConfig`` is frozen — assignment raises ValidationError."""
    from pydantic import ValidationError

    from nora.prompts.sync import OpenWebUIConfig

    config = OpenWebUIConfig(admin_api_key="dummy")
    with pytest.raises(ValidationError):
        config.model_base = "other-model"  # type: ignore[misc]


def test_config_requires_admin_api_key() -> None:
    """A blank ``admin_api_key`` is rejected at construction (fail-closed)."""
    from pydantic import ValidationError

    from nora.prompts.sync import OpenWebUIConfig

    with pytest.raises(ValidationError):
        OpenWebUIConfig()


# ---------------------------------------------------------------------------
# render_model_profile — body shape + watermark + metadata fields
# ---------------------------------------------------------------------------


def test_render_model_profile_uses_registry_render() -> None:
    """``params.system`` comes from ``registry.render(name)`` so the watermark is included.

    Q6 freezes the watermark banner; WU-3 builds the Open WebUI
    payload on top of ``render()``, not the bare ``body``, so the
    LLM running in Open WebUI sees the same provenance tag as
    embedded callers (the MCP server wrappers already switched in
    WU-2).

    The system prompt lives under ``params.system`` (Modelfile
    convention) per the verified Open WebUI API surface — a
    top-level ``system_prompt`` field is silently dropped by the
    server, so this test pins the nested path.
    """
    from nora.prompts.sync import OpenWebUIConfig, render_model_profile

    registry = _make_registry(
        [("netops_orchestrator", "0.3.5", "Test body line.\n")],
    )
    config = OpenWebUIConfig(admin_api_key="dummy", model_base="nora-netops")
    body = render_model_profile(
        registry,
        "netops_orchestrator",
        config,
        nora_version="0.3.5",
        git_sha="abcdef12",
        release_tag="v0.3.5",
    )
    system_field = body["params"]["system"]
    assert "Test body line" in system_field
    # Watermark banner (Q6 format).
    assert "NORA-PROMPT" in system_field
    assert "netops_orchestrator" in system_field
    assert "0.3.5" in system_field
    assert "sha:" in system_field


def test_render_model_profile_includes_metadata_fields() -> None:
    """The metadata dict carries ``commit_sha``, ``release_tag``, ``synced_at``,
    ``nora_version``, ``prompt_version``.

    Q7 freezes the metadata shape on both the immutable and mutable
    profiles. ``synced_at`` MUST be ISO-8601 UTC so the Open WebUI
    audit dashboard can sort by sync time without locale drift.
    """
    from nora.prompts.sync import OpenWebUIConfig, render_model_profile

    registry = _make_registry([("netops_orchestrator", "0.3.5", "Body.\n")])
    config = OpenWebUIConfig(admin_api_key="dummy", model_base="nora-netops")
    body = render_model_profile(
        registry,
        "netops_orchestrator",
        config,
        nora_version="0.3.5",
        git_sha="abcdef1234567890",
        release_tag="v0.3.5",
    )
    # The metadata block lives under the top-level "meta" field —
    # what Open WebUI consumes as the audit header.
    meta = body["meta"]
    assert meta["commit_sha"] == "abcdef1234567890"
    assert meta["release_tag"] == "v0.3.5"
    assert meta["nora_version"] == "0.3.5"
    assert meta["prompt_version"] == "0.3.5"
    # ISO-8601 UTC: `YYYY-MM-DDTHH:MM:SS(.fff)?(\+00:00|Z)`.
    assert re.match(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(\+00:00|Z)$",
        meta["synced_at"],
    ), f"synced_at must be ISO-8601 UTC; got {meta['synced_at']!r}"


def test_render_model_profile_includes_versioned_id() -> None:
    """The ``id`` field is ``<model_base>-v<X.Y.Z>`` for the immutable profile."""
    from nora.prompts.sync import OpenWebUIConfig, render_model_profile

    registry = _make_registry([("netops_orchestrator", "1.2.3", "Body.\n")])
    config = OpenWebUIConfig(admin_api_key="dummy", model_base="nora-netops")
    body = render_model_profile(
        registry,
        "netops_orchestrator",
        config,
        nora_version="0.3.5",
        git_sha="abc",
        release_tag=None,
    )
    assert body["id"] == "nora-netops-v1.2.3"


def test_render_model_profile_uses_custom_model_base() -> None:
    """``model_base`` overrides the default ``nora-netops`` prefix."""
    from nora.prompts.sync import OpenWebUIConfig, render_model_profile

    registry = _make_registry([("snmp_pmp450i", "0.3.5", "Body.\n")])
    config = OpenWebUIConfig(admin_api_key="dummy", model_base="custom-bot")
    body = render_model_profile(
        registry,
        "snmp_pmp450i",
        config,
        nora_version="0.3.5",
        git_sha="abc",
        release_tag=None,
    )
    assert body["id"] == "custom-bot-v0.3.5"
    assert body["name"] == "custom-bot-v0.3.5"


# ---------------------------------------------------------------------------
# sync_prompts — POST-to-create + POST-to-update orchestration via httpx.MockTransport
# ---------------------------------------------------------------------------


def test_sync_prompts_posts_versioned_profile() -> None:
    """MockTransport 200 → 2 POSTs/prompt (create + update), all POST, no PUT.

    Per Q7 and the verified Open WebUI API surface (PR #76 review
    comment by alexandervazquez98): the immutable
    ``<base>-v<X.Y.Z>`` profile is created via ``POST
    /api/v1/models/create``; the mutable ``<base>-latest`` alias
    is updated via ``POST /api/v1/models/model/update`` (id is
    carried in the body, not the URL). Both endpoints are POST — no
    PUT is used.
    """
    from nora.prompts.sync import (
        OpenWebUIConfig,
        OpenWebUISyncError,
        sync_prompts,
    )

    registry = _make_registry(
        [
            ("netops_orchestrator", "0.3.5", "Body-1.\n"),
            ("snmp_pmp450i", "0.3.5", "Body-2.\n"),
        ],
    )
    config = OpenWebUIConfig(admin_api_key="dummy", model_base="nora-netops")
    seen: list[httpx.Request] = []
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        bodies.append(json.loads(request.content.decode("utf-8")))
        # 200 == success on both create and update endpoints.
        # Open WebUI's create endpoint returns 200 or 201
        # (``httpx.codes.OK`` is the canonical accepted value here).
        return httpx.Response(httpx.codes.OK, json={"id": "ok"})

    def client_factory(cfg: OpenWebUIConfig) -> httpx.Client:
        return _make_mock_client(handler)

    results = sync_prompts(
        registry,
        config,
        nora_version="0.3.5",
        git_sha="abc",
        release_tag=None,
        client_factory=client_factory,
    )

    # 2 prompts × (POST create + POST update) = 4 wire requests.
    assert len(seen) == 4, (
        f"sync_prompts must POST create + POST update per prompt; got {len(seen)} "
        f"requests: methods={[r.method for r in seen]}"
    )
    methods = [r.method for r in seen]
    assert methods.count("POST") == 4, f"expected all 4 POSTs, got methods={methods}"
    # No PUT — the update endpoint takes POST with id in the body.
    assert "PUT" not in methods, (
        f"PUT must NOT appear (use POST /api/v1/models/model/update); got methods={methods}"
    )

    # Per-prompt interleaving: the create POST goes to /api/v1/models/create
    # and the update POST goes to /api/v1/models/model/update.
    urls = [str(r.url) for r in seen]
    assert urls[0].endswith("/api/v1/models/create"), (
        f"first POST must target /api/v1/models/create; got {urls[0]!r}"
    )
    assert urls[1].endswith("/api/v1/models/model/update"), (
        f"second POST must target /api/v1/models/model/update; got {urls[1]!r}"
    )
    assert urls[2].endswith("/api/v1/models/create"), (
        f"third POST must target /api/v1/models/create; got {urls[2]!r}"
    )
    assert urls[3].endswith("/api/v1/models/model/update"), (
        f"fourth POST must target /api/v1/models/model/update; got {urls[3]!r}"
    )

    # Aggregate URL pattern counts (defence-in-depth against silent routing
    # regressions — two creates + two updates must be observable).
    create_urls = [u for u in urls if "/api/v1/models/create" in u]
    update_urls = [u for u in urls if "/api/v1/models/model/update" in u]
    assert len(create_urls) == 2, f"expected 2 create POSTs; got {create_urls!r}"
    assert len(update_urls) == 2, f"expected 2 update POSTs; got {update_urls!r}"

    # Create POST bodies: params.system (NOT top-level system_prompt),
    # id starts with nora-netops-v<X.Y.Z>.
    create_bodies = [b for req, b in zip(seen, bodies) if "/api/v1/models/create" in str(req.url)]
    assert len(create_bodies) == 2
    for b in create_bodies:
        assert "system_prompt" not in b, (
            "top-level system_prompt is silently dropped by Open WebUI; "
            f"got keys={list(b.keys())!r}"
        )
        assert "params" in b and "system" in b["params"], (
            f"system prompt must live under params.system; got body={b!r}"
        )
        assert b["id"].startswith("nora-netops-v"), (
            f"create POST id must be versioned (nora-netops-v<X.Y.Z>); got {b['id']!r}"
        )
        assert "NORA-PROMPT" in b["params"]["system"], (
            "watermark banner must be present in params.system"
        )

    # Update POST bodies: id == nora-netops-latest (in the body, not URL).
    update_bodies = [
        b for req, b in zip(seen, bodies) if "/api/v1/models/model/update" in str(req.url)
    ]
    assert len(update_bodies) == 2
    for b in update_bodies:
        assert b["id"] == "nora-netops-latest", (
            f"update POST id must be the latest alias; got {b['id']!r}"
        )

    # Result shape — two result entries, all "created" (initial POST was 200).
    assert len(results) == 2
    for entry in results:
        assert entry["action"] == "created"
        assert entry["http_status"] == 200
        assert entry["version"] == "0.3.5"
    # Use OpenWebUISyncError to silence unused-import lint for the type.
    _ = OpenWebUISyncError


def test_sync_prompts_treats_duplicate_id_as_idempotent_success() -> None:
    """A 401 with the duplicate-id detail is treated as idempotent success (already_exists).

    Q7 says the immutable profile is created fresh per run. If the
    same NORA version is re-synced, Open WebUI rejects the create
    POST with HTTP 401 (NOT 409 Conflict as originally assumed)
    and body ``{"detail": "Model ID already taken"}`` because the
    id (``<base>-v<X.Y.Z>``) already exists. The orchestrator
    parses the response body and surfaces this as
    ``action="already_exists"`` so the operator's audit log shows
    the sync was a no-op rather than a failure — distinct from a
    real auth failure which surfaces ``OpenWebUIAuthError``.
    """
    from nora.prompts.sync import OpenWebUIConfig, sync_prompts

    registry = _make_registry([("netops_orchestrator", "0.3.5", "Body.\n")])
    config = OpenWebUIConfig(admin_api_key="dummy", model_base="nora-netops")

    def handler(request: httpx.Request) -> httpx.Response:
        # Dispatch by URL pattern (both endpoints are POST): the
        # create endpoint returns the duplicate-id detail, the
        # update endpoint succeeds. URL-based dispatch matches the
        # verified Open WebUI API surface where every mutation
        # verb is POST.
        url = str(request.url)
        if "/api/v1/models/create" in url:
            return httpx.Response(
                httpx.codes.UNAUTHORIZED,
                json={"detail": "Model ID already taken"},
            )
        return httpx.Response(httpx.codes.OK, json={"id": "ok"})

    def client_factory(cfg: OpenWebUIConfig) -> httpx.Client:
        return _make_mock_client(handler)

    results = sync_prompts(
        registry,
        config,
        nora_version="0.3.5",
        git_sha="abc",
        release_tag=None,
        client_factory=client_factory,
    )

    assert len(results) == 1
    entry = results[0]
    assert entry["action"] == "already_exists", (
        f"401 with duplicate-id detail must surface as already_exists; got {entry!r}"
    )
    assert entry["http_status"] == 401
    assert entry["name"] == "netops_orchestrator"
    assert entry["version"] == "0.3.5"


def test_sync_prompts_posts_to_update_endpoint_for_latest_alias() -> None:
    """The POST body to the update endpoint for ``<base>-latest`` carries the Q7 metadata fields.

    Per the verified Open WebUI API surface, the mutable alias is
    updated via ``POST /api/v1/models/model/update`` (id in body,
    not URL) — NOT ``PUT /api/v1/models/<id>``. The ``params.system``
    payload is the same body used for the immutable create POST;
    only the ``id``/``name`` are rewritten to the alias.
    """
    from nora.prompts.sync import OpenWebUIConfig, sync_prompts

    registry = _make_registry([("netops_orchestrator", "0.3.5", "Body.\n")])
    config = OpenWebUIConfig(admin_api_key="dummy", model_base="nora-netops")
    seen: list[httpx.Request] = []
    captured_bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        captured_bodies.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(httpx.codes.OK, json={"id": "ok"})

    def client_factory(cfg: OpenWebUIConfig) -> httpx.Client:
        return _make_mock_client(handler)

    sync_prompts(
        registry,
        config,
        nora_version="0.3.5",
        git_sha="deadbeef",
        release_tag="v0.3.5",
        client_factory=client_factory,
    )

    # Second wire request is the update POST (after the create POST at [0]).
    update_request = seen[1]
    assert update_request.method == "POST", (
        f"alias update must be POST (NOT PUT); got method={update_request.method!r}"
    )
    assert "/api/v1/models/model/update" in str(update_request.url), (
        f"alias update must hit /api/v1/models/model/update; got url={update_request.url!r}"
    )

    # Update POST body id matches the mutable alias (Q7).
    update_body = captured_bodies[1]
    assert update_body["id"] == "nora-netops-latest", (
        f"update POST body id must be nora-netops-latest; got {update_body['id']!r}"
    )
    # params.system (NOT top-level system_prompt).
    assert "system_prompt" not in update_body, (
        "top-level system_prompt is silently dropped by Open WebUI; "
        f"got keys={list(update_body.keys())!r}"
    )
    assert "params" in update_body and "system" in update_body["params"], (
        f"update body must carry params.system; got {update_body!r}"
    )
    # Metadata fields are carried through to the alias update.
    assert update_body["meta"]["commit_sha"] == "deadbeef"
    assert update_body["meta"]["release_tag"] == "v0.3.5"
    assert update_body["meta"]["nora_version"] == "0.3.5"


def test_sync_prompts_raises_on_auth_failure_401() -> None:
    """A NON-duplicate 401 from server → ``OpenWebUIAuthError``.

    Per the verified Open WebUI API surface: a 401 with body
    ``{"detail": "Model ID already taken"}`` is the duplicate-id
    no-op signal and is treated as ``action="already_exists"`` (see
    ``test_sync_prompts_treats_duplicate_id_as_idempotent_success``).
    Any other 401 body — e.g. ``{"detail": "Invalid token"}`` from a
    missing or wrong ``admin_api_key`` — is a real auth failure and
    surfaces ``OpenWebUIAuthError`` so the operator must fix the
    credentials.
    """
    from nora.prompts.sync import (
        OpenWebUIAuthError,
        OpenWebUIConfig,
        sync_prompts,
    )

    registry = _make_registry([("netops_orchestrator", "0.3.5", "Body.\n")])
    config = OpenWebUIConfig(admin_api_key="dummy", model_base="nora-netops")

    def handler(request: httpx.Request) -> httpx.Response:
        # NON-duplicate 401: a real auth failure (missing/wrong token),
        # NOT the "Model ID already taken" duplicate-id detail.
        return httpx.Response(httpx.codes.UNAUTHORIZED, json={"detail": "Invalid token"})

    def client_factory(cfg: OpenWebUIConfig) -> httpx.Client:
        return _make_mock_client(handler)

    with pytest.raises(OpenWebUIAuthError):
        sync_prompts(
            registry,
            config,
            nora_version="0.3.5",
            git_sha="abc",
            release_tag=None,
            client_factory=client_factory,
        )


def test_sync_prompts_raises_on_403() -> None:
    """403 from server → ``OpenWebUIAuthError`` (auth-or-permission)."""
    from nora.prompts.sync import (
        OpenWebUIAuthError,
        OpenWebUIConfig,
        sync_prompts,
    )

    registry = _make_registry([("netops_orchestrator", "0.3.5", "Body.\n")])
    config = OpenWebUIConfig(admin_api_key="dummy", model_base="nora-netops")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(httpx.codes.FORBIDDEN, json={"detail": "forbidden"})

    def client_factory(cfg: OpenWebUIConfig) -> httpx.Client:
        return _make_mock_client(handler)

    with pytest.raises(OpenWebUIAuthError):
        sync_prompts(
            registry,
            config,
            nora_version="0.3.5",
            git_sha="abc",
            release_tag=None,
            client_factory=client_factory,
        )


def test_sync_prompts_raises_on_500() -> None:
    """5xx from server → ``OpenWebUISyncError`` (transport-class error)."""
    from nora.prompts.sync import (
        OpenWebUIConfig,
        OpenWebUISyncError,
        sync_prompts,
    )

    registry = _make_registry([("netops_orchestrator", "0.3.5", "Body.\n")])
    config = OpenWebUIConfig(admin_api_key="dummy", model_base="nora-netops")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(httpx.codes.INTERNAL_SERVER_ERROR, json={"detail": "boom"})

    def client_factory(cfg: OpenWebUIConfig) -> httpx.Client:
        return _make_mock_client(handler)

    with pytest.raises(OpenWebUISyncError):
        sync_prompts(
            registry,
            config,
            nora_version="0.3.5",
            git_sha="abc",
            release_tag=None,
            client_factory=client_factory,
        )


def test_sync_prompts_skips_tool_specs() -> None:
    """Tool-specs (no ``version`` + ``checksum_sha256``) are not synced.

    ``PromptRegistry.from_settings`` includes BOTH system prompts
    (``src/nora/prompts/*.md``) and tool specs (``docs/tool_specs/*.md``).
    Only the system prompts are versioned, so tool specs MUST be
    filtered out — otherwise ``render_model_profile`` would emit
    bare bodies with no version metadata.
    """
    from nora.prompts.sync import OpenWebUIConfig, sync_prompts

    # Mix one system-prompt (version + checksum) and one tool-spec
    # (only tier metadata, no version).
    system_prompt = Prompt(
        name="netops_orchestrator",
        description="system prompt",
        body="body\n",
        metadata={
            "version": "0.3.5",
            "checksum_sha256": "0" * 64,
        },
    )
    tool_spec = Prompt(
        name="snmp_get_ap_summary",
        description="tool spec",
        body="tool body\n",
        metadata={"tier": 0, "requires_operator_confirmed": False, "requires_hitl_token": False},
    )
    registry = PromptRegistry(
        _prompts={
            system_prompt.name: system_prompt,
            tool_spec.name: tool_spec,
        },
        _source_dirs=(),
    )
    config = OpenWebUIConfig(admin_api_key="dummy", model_base="nora-netops")
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(httpx.codes.OK, json={"id": "ok"})

    def client_factory(cfg: OpenWebUIConfig) -> httpx.Client:
        return _make_mock_client(handler)

    results = sync_prompts(
        registry,
        config,
        nora_version="0.3.5",
        git_sha="abc",
        release_tag=None,
        client_factory=client_factory,
    )

    # Only the system prompt was synced.
    assert len(results) == 1
    assert results[0]["name"] == "netops_orchestrator"
    # 1 system prompt × (POST create + POST update) = 2 wire requests.
    assert len(captured) == 2


# ---------------------------------------------------------------------------
# sync_prompts — duplicate-id detail string variation (PR #76 sandbox)
# ---------------------------------------------------------------------------


def test_sync_prompts_treats_already_registered_detail_as_idempotent() -> None:
    """HTTP 401 with body containing 'already registered' (Open WebUI MODEL_ID_TAKEN
    constant at constants.py:55) is also treated as the idempotent ``already_exists``
    signal.

    Per PR #76 post-fix sandbox comment: Open WebUI's actual duplicate-id
    error string is ``"Uh-oh! This model id is already registered. Please
    choose another model id string."`` (constants.py:55). The original
    implementation only matched ``"already taken"`` (the spec's original
    text). This test pins the broader match so future Open WebUI text
    variations don't silently break idempotency classification.
    """
    from nora.prompts.sync import OpenWebUIConfig, sync_prompts

    registry = _make_registry([("netops_orchestrator", "0.3.5", "Body.\n")])
    config = OpenWebUIConfig(admin_api_key="dummy", model_base="nora-netops")

    def handler(request: httpx.Request) -> httpx.Response:
        # Create POST returns 401 with the Open WebUI source-constant text.
        if "/api/v1/models/create" in str(request.url):
            return httpx.Response(
                httpx.codes.UNAUTHORIZED,
                json={
                    "detail": (
                        "Uh-oh! This model id is already registered. "
                        "Please choose another model id string."
                    )
                },
            )
        return httpx.Response(httpx.codes.OK, json={"id": "ok"})

    def client_factory(cfg: OpenWebUIConfig) -> httpx.Client:
        return _make_mock_client(handler)

    results = sync_prompts(
        registry,
        config,
        nora_version="0.3.5",
        git_sha="abc",
        release_tag=None,
        client_factory=client_factory,
    )

    assert len(results) == 1
    entry = results[0]
    assert entry["action"] == "already_exists", (
        f"401 with 'already registered' must surface as already_exists; got {entry!r}"
    )
    assert entry["http_status"] == 401
    assert entry["name"] == "netops_orchestrator"
    assert entry["version"] == "0.3.5"


# ---------------------------------------------------------------------------
# sync_prompts — upsert fallback for first-time mutable alias (PR #76 sandbox)
# ---------------------------------------------------------------------------


def test_sync_prompts_falls_back_to_create_when_alias_does_not_exist() -> None:
    """First-time sync for the mutable alias seeds via POST /create when
    POST /update returns 404 NOT_FOUND with the alias-not-found detail.

    Per PR #76 post-fix sandbox comment: a fresh deployment where
    ``<model_base>-latest`` does not yet exist yields HTTP 404 on
    ``POST /api/v1/models/model/update`` with body
    ``{"detail": "We could not find what you're looking for :/"}``.
    The orchestrator catches this and POSTs again to the create endpoint
    with the same body, so the alias is seeded without operator action.
    """
    from nora.prompts.sync import OpenWebUIConfig, sync_prompts

    registry = _make_registry([("netops_orchestrator", "0.3.5", "Body.\n")])
    config = OpenWebUIConfig(admin_api_key="dummy", model_base="nora-netops")
    seen: list[httpx.Request] = []
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        bodies.append(json.loads(request.content.decode("utf-8")))
        url = str(request.url)
        if "/api/v1/models/create" in url:
            # Create always succeeds (both versioned profile AND alias fallback).
            return httpx.Response(httpx.codes.OK, json={"id": "ok"})
        if "/api/v1/models/model/update" in url:
            # Alias does not exist yet — first-time-sync NOT_FOUND.
            return httpx.Response(
                httpx.codes.NOT_FOUND,
                json={"detail": "We could not find what you're looking for :/"},
            )
        # Defensive default — should not reach here.
        return httpx.Response(httpx.codes.OK, json={"id": "ok"})

    def client_factory(cfg: OpenWebUIConfig) -> httpx.Client:
        return _make_mock_client(handler)

    # Should NOT raise — the upsert fallback should seed the alias.
    results = sync_prompts(
        registry,
        config,
        nora_version="0.3.5",
        git_sha="abc",
        release_tag=None,
        client_factory=client_factory,
    )

    assert len(results) == 1
    assert results[0]["action"] == "created"
    assert results[0]["http_status"] == 200

    # Wire-call sequence:
    # 1. POST /api/v1/models/create (versioned profile) — 200
    # 2. POST /api/v1/models/model/update (alias first try) — 404
    # 3. POST /api/v1/models/create (alias fallback) — 200
    assert len(seen) == 3, (
        f"upsert fallback expected 3 wire calls (create + update + create), got {len(seen)}: "
        f"urls={[str(r.url) for r in seen]}"
    )
    urls = [str(r.url) for r in seen]
    assert urls[0].endswith("/api/v1/models/create")
    assert urls[1].endswith("/api/v1/models/model/update")
    assert urls[2].endswith("/api/v1/models/create"), (
        f"third wire call must be the alias fallback to /create; got {urls[2]}"
    )
    # The alias fallback body MUST carry the alias id (not the versioned id).
    assert bodies[2]["id"] == "nora-netops-latest"


def test_sync_prompts_falls_back_to_create_when_alias_returns_401_not_found() -> None:
    """First-time sync for the mutable alias seeds via POST /create when
    POST /update returns HTTP 401 UNAUTHORIZED with the not-found detail
    (the ACTUAL server behaviour per Open WebUI's routers/models.py:785).

    Per PR #76 round-3 sandbox comment: Open WebUI's update router
    returns ``HTTPException(status_code=HTTP_401_UNAUTHORIZED,
    detail=ERROR_MESSAGES.NOT_FOUND)`` for a missing record, NOT 404
    NOT_FOUND as the original implementation assumed. The
    ``_is_alias_not_found_response`` helper now accepts BOTH status
    codes, and ``_check_update_status`` checks the alias-not-found
    pattern BEFORE the auth-failure check, so a 401 with the
    not-found detail triggers the upsert fallback rather than
    raising ``OpenWebUIAuthError``.

    This test is the operational twin of
    ``test_sync_prompts_falls_back_to_create_when_alias_does_not_exist``
    (which mocks the 404 path). Both must pass.
    """
    from nora.prompts.sync import OpenWebUIConfig, sync_prompts

    registry = _make_registry([("netops_orchestrator", "0.3.5", "Body.\n")])
    config = OpenWebUIConfig(admin_api_key="dummy", model_base="nora-netops")
    seen: list[httpx.Request] = []
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        bodies.append(json.loads(request.content.decode("utf-8")))
        url = str(request.url)
        if "/api/v1/models/create" in url:
            # Create succeeds for both the versioned profile and the alias fallback.
            return httpx.Response(httpx.codes.OK, json={"id": "ok"})
        if "/api/v1/models/model/update" in url:
            # Open WebUI returns HTTP 401 + the not-found detail for a missing record.
            return httpx.Response(
                httpx.codes.UNAUTHORIZED,
                json={"detail": "We could not find what you're looking for :/"},
            )
        return httpx.Response(httpx.codes.OK, json={"id": "ok"})

    def client_factory(cfg: OpenWebUIConfig) -> httpx.Client:
        return _make_mock_client(handler)

    # Should NOT raise — the upsert fallback should seed the alias via /create.
    results = sync_prompts(
        registry,
        config,
        nora_version="0.3.5",
        git_sha="abc",
        release_tag=None,
        client_factory=client_factory,
    )

    assert len(results) == 1
    assert results[0]["action"] == "created"
    assert results[0]["http_status"] == 200

    # Wire-call sequence:
    # 1. POST /api/v1/models/create (versioned profile) — 200
    # 2. POST /api/v1/models/model/update (alias first try) — 401 with not-found detail
    # 3. POST /api/v1/models/create (alias fallback) — 200
    assert len(seen) == 3, (
        f"upsert fallback expected 3 wire calls (create + update + create), got {len(seen)}: "
        f"methods={[r.method for r in seen]}, urls={[str(r.url) for r in seen]}"
    )
    urls = [str(r.url) for r in seen]
    assert urls[0].endswith("/api/v1/models/create")
    assert urls[1].endswith("/api/v1/models/model/update")
    assert urls[2].endswith("/api/v1/models/create"), (
        f"third wire call must be the alias fallback to /create; got {urls[2]}"
    )
    # The alias fallback body MUST carry the alias id (not the versioned id).
    assert bodies[2]["id"] == "nora-netops-latest"


__all__ = [
    "test_config_defaults",
    "test_config_strips_trailing_slash_from_base_url",
    "test_config_is_frozen",
    "test_config_requires_admin_api_key",
    "test_render_model_profile_uses_registry_render",
    "test_render_model_profile_includes_metadata_fields",
    "test_render_model_profile_includes_versioned_id",
    "test_render_model_profile_uses_custom_model_base",
    "test_sync_prompts_falls_back_to_create_when_alias_does_not_exist",
    "test_sync_prompts_falls_back_to_create_when_alias_returns_401_not_found",
    "test_sync_prompts_posts_to_update_endpoint_for_latest_alias",
    "test_sync_prompts_posts_versioned_profile",
    "test_sync_prompts_raises_on_auth_failure_401",
    "test_sync_prompts_raises_on_403",
    "test_sync_prompts_raises_on_500",
    "test_sync_prompts_skips_tool_specs",
    "test_sync_prompts_treats_already_registered_detail_as_idempotent",
    "test_sync_prompts_treats_duplicate_id_as_idempotent_success",
]
