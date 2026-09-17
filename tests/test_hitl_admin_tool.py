"""Tests for the `hitl_mint_token` MCP admin tool — WU-4 / PR #44.

The tool exposes ``nora hitl mint`` semantics over the MCP wire so NOC
operators without terminal access to the backend host can mint
verification tokens from chat. It delegates to
:func:`nora.hitl.tokens.mint_token` using the boot-time
``Settings.nora_hitl_signing_key``; the wire response is a
``HitlApprovalToken.model_dump(mode="json")`` envelope.

Scenarios covered:

* :func:`test_hitl_mint_token_happy_path` — valid signing key produces
  a typed ``HitlApprovalToken`` envelope with a non-empty 64-char
  hex signature, ``token`` prefix ``hitl-<operator_id>-``, and
  ``expires_at = issued_at + 900s`` by default.
* :func:`test_hitl_mint_token_refuses_when_signing_key_missing` —
  missing signing key raises ``AutonomousMutationRejected`` (lazy
  fail-closed contract; mirrors :func:`verify_approval_token`).
* :func:`test_hitl_mint_token_json_envelope_roundtrip` — custom TTL
  is honoured and the envelope survives a JSON round-trip byte-for-byte.

The scenarios anchor behaviour against
:func:`nora.hitl.tokens.mint_token` directly AND exercise the
``@mcp.tool`` registration through the FastMCP ``Client`` so the
tool-decorator wiring stays locked-in.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from pydantic import SecretStr

# ---------------------------------------------------------------------------
# Anchor — direct library behaviour (mirrors tests/test_hitl_tokens.py).
# ---------------------------------------------------------------------------


def test_hitl_mint_token_happy_path() -> None:
    """`mint_token('tester', signing_key=SecretStr('test-key'))` is well-formed.

    The anchor for the tool wrapper: a valid signing key produces a
    typed ``HitlApprovalToken`` carrying a non-empty ``token``,
    ``operator_id``, ``signature`` (64-char SHA-256 hex), and
    ``expires_at = issued_at + 900s`` (the default TTL).
    """
    from nora.hitl.tokens import mint_token

    token = mint_token("tester", signing_key=SecretStr("test-key"))

    assert token.token, "token must be non-empty"
    assert token.token.startswith("hitl-tester-"), (
        f"token must begin with 'hitl-tester-'; got {token.token!r}"
    )
    assert token.operator_id == "tester"
    assert token.signature, "signature must be non-empty"
    assert len(token.signature) == 64, (
        f"signature must be a 64-char SHA-256 hex digest; got len={len(token.signature)}"
    )

    # Default TTL is 900s; expires_at - issued_at == 900s (±2s tolerance
    # for monotonic-clock skew on slow test runners).
    delta_seconds = (token.expires_at - token.issued_at).total_seconds()
    assert abs(delta_seconds - 900.0) < 2.0, (
        f"default TTL must be 900s (issued_at -> expires_at); got {delta_seconds}s"
    )

    # The wire-shape envelope (what `@mcp.tool hitl_mint_token` returns).
    envelope = token.model_dump(mode="json")
    assert envelope["token"] == token.token
    assert envelope["operator_id"] == "tester"
    assert envelope["signature"] == token.signature
    # Pydantic v2's JSON-mode dump normalises tzinfo to ``Z``; the
    # in-memory ``datetime.isoformat()`` emits ``+00:00``. Both
    # representations point at the same instant, so parse them
    # back to a ``datetime`` to compare.
    assert datetime.fromisoformat(envelope["issued_at"].replace("Z", "+00:00")) == (
        token.issued_at.astimezone(timezone.utc)
    )
    assert datetime.fromisoformat(envelope["expires_at"].replace("Z", "+00:00")) == (
        token.expires_at.astimezone(timezone.utc)
    )


def test_hitl_mint_token_refuses_when_signing_key_missing() -> None:
    """`mint_token(..., signing_key=None)` raises `AutonomousMutationRejected`.

    Lazy fail-closed contract: the tool must reject on missing or
    empty signing key (mirrors :func:`verify_approval_token`). The
    literal message is the contract seam pinned by
    ``tests/test_hitl_tokens.py``.
    """
    from nora.drivers.exceptions import AutonomousMutationRejected
    from nora.hitl.tokens import mint_token

    expected = "autonomous device mutation rejected: HITL approval token required"

    # None — the default when the operator did not configure the env var.
    with __import__("pytest").raises(AutonomousMutationRejected) as exc_info:
        mint_token("tester", signing_key=None)
    assert str(exc_info.value) == expected, (
        f"missing signing key MUST raise the literal HITL message; got {str(exc_info.value)!r}"
    )

    # Empty string — the operator wired an env var with no value.
    with __import__("pytest").raises(AutonomousMutationRejected) as exc_info:
        mint_token("tester", signing_key="")
    assert str(exc_info.value) == expected, (
        f"empty signing key MUST raise the literal HITL message; got {str(exc_info.value)!r}"
    )


def test_hitl_mint_token_json_envelope_roundtrip() -> None:
    """Custom TTL is honoured; envelope survives a JSON round-trip byte-for-byte.

    The wire response from the MCP tool is a JSON-serialisable dict;
    a downstream consumer (e.g. an LLM agent feeding the token into
    ``snmp_migrate_radio_frequency``) must be able to round-trip
    ``json.dumps(...)`` -> ``json.loads(...)`` without any field drift.
    The signature stays byte-identical because the canonical payload
    tuple is fully determined by the model fields.
    """
    from nora.hitl.tokens import mint_token

    token = mint_token(
        "roundtrip",
        ttl_seconds=60,
        signing_key=SecretStr("another-key"),
    )
    envelope = token.model_dump(mode="json")
    raw = json.dumps(envelope)
    parsed = json.loads(raw)

    # All keys preserved.
    assert set(parsed.keys()) == {
        "token",
        "operator_id",
        "issued_at",
        "expires_at",
        "signature",
    }, f"envelope keys drifted across JSON round-trip; got {sorted(parsed.keys())!r}"

    # Values match verbatim.
    assert parsed["operator_id"] == "roundtrip"
    assert "roundtrip" in parsed["token"], (
        f"token MUST embed the operator id; got {parsed['token']!r}"
    )
    assert parsed["signature"] == token.signature

    # Custom TTL honoured: expires_at - issued_at == 60s (±2s tolerance).
    issued_at = datetime.fromisoformat(parsed["issued_at"])
    expires_at = datetime.fromisoformat(parsed["expires_at"])
    delta_seconds = (expires_at - issued_at).total_seconds()
    assert abs(delta_seconds - 60.0) < 2.0, f"custom TTL must be 60s; got {delta_seconds}s"


# ---------------------------------------------------------------------------
# @mcp.tool wrapper — exercise through the FastMCP Client.
# ---------------------------------------------------------------------------


def test_mcp_hitl_mint_tool_wrapper_returns_typed_envelope() -> None:
    """The `@mcp.tool hitl_mint_token` wrapper delegates to `mint_token`.

    Drives the tool through the FastMCP `Client` so the
    `@mcp.tool` decorator wiring is locked-in alongside the library
    behaviour. Sets `nora_hitl_signing_key` via the boot-time
    `Settings` injection; asserts the wire response is a
    `HitlApprovalToken.model_dump(mode="json")` envelope.
    """
    import asyncio

    from nora import server as server_mod
    from nora.config import Settings

    settings = Settings(
        _env_file=None,
        _env_file_encoding=None,
        nora_hitl_signing_key=SecretStr("wrapper-key"),
    )
    server_mod.set_runtime_state(settings)

    async def _call() -> dict:
        from fastmcp import Client

        async with Client(server_mod.mcp) as client:
            result = await client.call_tool(
                "hitl_mint_token",
                {"operator_id": "wrapper-tester", "ttl_seconds": 900},
            )
            return result.data  # type: ignore[no-any-return]

    envelope = asyncio.run(_call())

    assert isinstance(envelope, dict), f"tool MUST return a dict; got {type(envelope)}"
    assert set(envelope.keys()) == {
        "token",
        "operator_id",
        "issued_at",
        "expires_at",
        "signature",
    }
    assert envelope["operator_id"] == "wrapper-tester"
    assert envelope["token"].startswith("hitl-wrapper-tester-")
    assert len(envelope["signature"]) == 64


def test_mcp_hitl_mint_tool_wrapper_refuses_missing_signing_key() -> None:
    """The `@mcp.tool` wrapper surfaces `AutonomousMutationRejected` to MCP callers.

    No boot-time signing key — the tool must fail closed with the
    typed exception. FastMCP wraps the raised exception as a
    :class:`fastmcp.exceptions.ToolError` carrying the literal HITL
    message verbatim.
    """
    import asyncio

    from fastmcp.exceptions import ToolError

    from nora import server as server_mod
    from nora.config import Settings

    settings = Settings(
        _env_file=None,
        _env_file_encoding=None,
        nora_hitl_signing_key=None,
    )
    server_mod.set_runtime_state(settings)

    async def _call() -> None:
        from fastmcp import Client

        async with Client(server_mod.mcp) as client:
            await client.call_tool(
                "hitl_mint_token",
                {"operator_id": "no-key"},
            )

    expected = "autonomous device mutation rejected: HITL approval token required"
    with __import__("pytest").raises(ToolError) as exc_info:
        asyncio.run(_call())
    assert expected in str(exc_info.value), (
        f"ToolError MUST carry the literal HITL message; got {str(exc_info.value)!r}"
    )


# ---------------------------------------------------------------------------
# Settings — the gate field exists and defaults to ON.
# ---------------------------------------------------------------------------


def test_settings_default_has_hitl_admin_enabled_on() -> None:
    """`Settings()` defaults `nora_hitl_admin_enabled` to True (WU-4 user decision).

    Per `odd/tasks/pr44-followups.md` confirmed decision 2026-09-17:
    the admin tool is opt-OUT (set ``NORA_HITL_ADMIN_ENABLED=false``
    to force CLI-only minting); the field MUST default to True so
    NOC operators without terminal access can mint from chat.
    Operators who want to disable set ``NORA_HITL_ADMIN_ENABLED=false``.
    """
    from nora.config import Settings

    settings = Settings(
        _env_file=None,
        _env_file_encoding=None,
    )
    assert settings.nora_hitl_admin_enabled is True, (
        f"nora_hitl_admin_enabled MUST default to True; got {settings.nora_hitl_admin_enabled!r}"
    )


def test_settings_hitl_admin_enabled_respects_env_var() -> None:
    """`NORA_HITL_ADMIN_ENABLED=false` flips the gate off via Pydantic env binding.

    The `BaseSettings` `env_file` precedence is `.env` > process env;
    we pass `_env_file=None` so the process env is the only source.
    """
    import pytest

    from nora.config import Settings

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("NORA_HITL_ADMIN_ENABLED", "false")
        settings = Settings(
            _env_file=None,
            _env_file_encoding=None,
        )
    assert settings.nora_hitl_admin_enabled is False, (
        f"NORA_HITL_ADMIN_ENABLED=false MUST round-trip to False; "
        f"got {settings.nora_hitl_admin_enabled!r}"
    )
