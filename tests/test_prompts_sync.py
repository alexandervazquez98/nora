"""Tests for the Open WebUI declarative sync layer — issue #45 / WU-3.

Maps the prompt-versioning WU-3 contract:

* ``OpenWebUIConfig`` — frozen Pydantic model that strips trailing ``/``
  from ``base_url`` and carries the operator-overridable defaults
  (``http://localhost:8080``, ``nora-netops``, 30s, 2 retries).
* ``render_model_profile`` — builds the JSON body for one prompt:
  ``{id, name, meta, params, system_prompt}`` plus the Q7 metadata
  block (``commit_sha``, ``release_tag``, ``synced_at``, ``nora_version``,
  ``prompt_version``). Pulls the body via ``registry.render(name)`` so
  the watermark banner is included.
* ``sync_prompts`` — orchestrates a POST for the immutable
  ``<base>-v<X.Y.Z>`` profile (409 = idempotent success) and a PUT
  for the mutable ``<base>-latest`` alias. Returns one result dict
  per prompt with ``action in {"created", "already_exists", "updated"}``.
* Exception contract — typed errors on 401/403/5xx; 409 is success.

Zero-Leakage: ``http://testnet`` / ``192.0.2.x`` style addresses only.
Hermeticity: ``httpx.MockTransport`` wired into ``build_client`` via
the optional ``client_factory`` injection point — no real network,
no ``.env`` mutation, no console scripts.

The Open WebUI REST surface used here (PUT to ``/api/v1/models`` for
mutating an existing record vs. POST for creation) is the documented
behavior of Open WebUI's ``models.create`` / ``models.update``
endpoints; the SPEC pins POST for *new* model creation only
(immutable profiles), and Open WebUI's general PUT-mutates-resource
contract for the mutable alias. See the assumption notes in
``sync.render_model_profile``'s docstring.
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
    """``system_prompt`` comes from ``registry.render(name)`` so the watermark is included.

    Q6 freezes the watermark banner; WU-3 builds the Open WebUI
    payload on top of ``render()``, not the bare ``body``, so the
    LLM running in Open WebUI sees the same provenance tag as
    embedded callers (the MCP server wrappers already switched in
    WU-2).
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
    system_prompt_field = body["system_prompt"]
    assert "Test body line" in system_prompt_field
    # Watermark banner (Q6 format).
    assert "NORA-PROMPT" in system_prompt_field
    assert "netops_orchestrator" in system_prompt_field
    assert "0.3.5" in system_prompt_field
    assert "sha:" in system_prompt_field


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
# sync_prompts — POST/PUT orchestration via httpx.MockTransport
# ---------------------------------------------------------------------------


def test_sync_prompts_posts_versioned_profile() -> None:
    """MockTransport returns 200 → exactly one POST per registered prompt, body id is versioned.

    Per Q7, the immutable profile is created via POST. The PUT for
    the mutable ``<base>-latest`` alias follows on the SAME body so
    the alias tracks the latest synced tag.
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
        # POST returns 200 (treated as success), PUT returns 200 too.
        # Whether Open WebUI returns 200 or 201 on create is documented
        # as \"either is valid\"; ``httpx.codes.OK`` is the canonical
        # accepted value here.
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

    # 2 prompts × (POST + PUT) = 4 requests.
    assert len(seen) == 4, (
        f"sync_prompts must POST + PUT per prompt; got {len(seen)} requests: "
        f"methods={[r.method for r in seen]}"
    )
    methods = [r.method for r in seen]
    assert methods.count("POST") == 2, f"expected exactly 2 POSTs, got methods={methods}"
    assert methods.count("PUT") == 2, f"expected exactly 2 PUTs, got methods={methods}"

    # Each POST body should carry the versioned id (Q7 immutable profile).
    post_bodies = [b for req, b in zip(seen, bodies) if req.method == "POST"]
    post_ids = sorted(b["id"] for b in post_bodies)
    assert post_ids == ["nora-netops-v0.3.5", "nora-netops-v0.3.5"], (
        f"versioned POST ids should be nora-netops-v0.3.5 (with one per prompt), got {post_ids}"
    )

    # `latest` PUT body id is the mutable alias.
    put_bodies = [b for req, b in zip(seen, bodies) if req.method == "PUT"]
    put_ids = sorted(b["id"] for b in put_bodies)
    assert put_ids == ["nora-netops-latest", "nora-netops-latest"], (
        f"latest PUT ids should be nora-netops-latest, got {put_ids}"
    )

    # Result shape — two result entries, all "created" (initial POST was 200).
    assert len(results) == 2
    for entry in results:
        assert entry["action"] == "created"
        assert entry["http_status"] == 200
        assert entry["version"] == "0.3.5"
    # Use OpenWebUISyncError to silence unused-import lint for the type.
    _ = OpenWebUISyncError


def test_sync_prompts_treats_409_as_idempotent_success() -> None:
    """A 409 from POST is treated as idempotent success (already_exists).

    Q7 says the immutable profile is created fresh per run. If the
    same NORA version is re-synced, Open WebUI rejects the POST
    with 409 Conflict because the id (``<base>-v<X.Y.Z>``) already
    exists. We surface this as ``action="already_exists"`` so the
    operator's audit log shows the sync was a no-op rather than a
    failure.
    """
    from nora.prompts.sync import OpenWebUIConfig, sync_prompts

    registry = _make_registry([("netops_orchestrator", "0.3.5", "Body.\n")])
    config = OpenWebUIConfig(admin_api_key="dummy", model_base="nora-netops")

    def handler(request: httpx.Request) -> httpx.Response:
        # POST returns 409 (already exists), PUT still succeeds.
        if request.method == "POST":
            return httpx.Response(httpx.codes.CONFLICT, json={"detail": "exists"})
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
    assert entry["action"] == "already_exists", f"409 must surface as already_exists; got {entry!r}"
    assert entry["http_status"] == 409
    assert entry["name"] == "netops_orchestrator"
    assert entry["version"] == "0.3.5"


def test_sync_prompts_puts_latest_alias() -> None:
    """The PUT body for ``<base>-latest`` carries the same Q7 metadata fields."""
    from nora.prompts.sync import OpenWebUIConfig, sync_prompts

    registry = _make_registry([("netops_orchestrator", "0.3.5", "Body.\n")])
    config = OpenWebUIConfig(admin_api_key="dummy", model_base="nora-netops")
    captured_bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
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

    put_body = next(b for i, b in enumerate(captured_bodies) if i == 1)
    # PUT body id matches the mutable alias (Q7).
    assert put_body["id"] == "nora-netops-latest"
    # Metadata fields are carried through to the alias PUT.
    assert put_body["meta"]["commit_sha"] == "deadbeef"
    assert put_body["meta"]["release_tag"] == "v0.3.5"
    assert put_body["meta"]["nora_version"] == "0.3.5"


def test_sync_prompts_raises_on_401() -> None:
    """401 from server → ``OpenWebUIAuthError``."""
    from nora.prompts.sync import (
        OpenWebUIAuthError,
        OpenWebUIConfig,
        sync_prompts,
    )

    registry = _make_registry([("netops_orchestrator", "0.3.5", "Body.\n")])
    config = OpenWebUIConfig(admin_api_key="dummy", model_base="nora-netops")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(httpx.codes.UNAUTHORIZED, json={"detail": "auth"})

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
    # 1 system prompt × (POST + PUT) = 2 wire requests.
    assert len(captured) == 2


__all__ = [
    "test_config_defaults",
    "test_config_strips_trailing_slash_from_base_url",
    "test_config_is_frozen",
    "test_config_requires_admin_api_key",
    "test_render_model_profile_uses_registry_render",
    "test_render_model_profile_includes_metadata_fields",
    "test_render_model_profile_includes_versioned_id",
    "test_render_model_profile_uses_custom_model_base",
    "test_sync_prompts_posts_versioned_profile",
    "test_sync_prompts_treats_409_as_idempotent_success",
    "test_sync_prompts_puts_latest_alias",
    "test_sync_prompts_raises_on_401",
    "test_sync_prompts_raises_on_403",
    "test_sync_prompts_raises_on_500",
    "test_sync_prompts_skips_tool_specs",
]
