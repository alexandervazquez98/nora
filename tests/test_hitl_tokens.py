"""Tests for the HITL approval-token verifier — PR 4 slice 4 commit 1.

These tests pin the contract seam that the slice-4 RF migration
tool (``snmp_migrate_radio_frequency``) calls BEFORE any SNMP SET
frame is emitted:

* :func:`nora.hitl.tokens.verify_approval_token` raises
  :class:`AutonomousMutationRejected` with the **literal** message
  ``"autonomous device mutation rejected: HITL approval token
  required"`` when the token is missing, empty, syntactically
  invalid, expired, or kill-switched via
  ``NORA_HITL_TOKEN_TTL_SECONDS=0``.
* A non-empty, well-formed, unexpired token round-trips through
  :func:`nora.hitl.tokens.mint_token` AND
  :func:`nora.hitl.tokens.verify_approval_token` without raising.

Named tests for PR 4 (slice 4 commit 1):

* ``test_migrate_autonomous_call_raises_autonomous_mutation_rejected``
* ``test_migrate_requires_hitl_approval_token``
* ``test_hitl_mint_and_verify_round_trip``
* ``test_hitl_kill_switch_rejects_via_env_var``

Zero-Leakage: only TEST-NET-1 (``192.0.2.x``) host literals; no real
IPs, hostnames, serials, or credentials.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

# ---------------------------------------------------------------------------
# Shared fixtures — hermetic token factory.
# ---------------------------------------------------------------------------


def _encode_token(
    *,
    operator_id: str = "tester",
    ttl_seconds: int = 900,
    expires_at: datetime | None = None,
) -> str:
    """Build a JSON token string compatible with the stub verifier.

    The stub reads a JSON payload of the form
    ``{"token": ..., "operator_id": ..., "issued_at": ..., "expires_at": ...}``.
    Exposed as a helper so tests can construct expired / kill-switched
    variants without rebuilding the format from scratch.
    """
    now = datetime.now(timezone.utc)
    expiry = expires_at if expires_at is not None else (now + timedelta(seconds=ttl_seconds))
    payload = {
        "token": f"stub-{operator_id}",
        "operator_id": operator_id,
        "issued_at": now.isoformat(),
        "expires_at": expiry.isoformat(),
    }
    return json.dumps(payload)


# ---------------------------------------------------------------------------
# Named test #1 — migrate_autonomous_call_raises_autonomous_mutation_rejected
# ---------------------------------------------------------------------------


def test_migrate_autonomous_call_raises_autonomous_mutation_rejected() -> None:
    """``verify_approval_token(None)`` raises the literal exception.

    Per `pmp450i-radio-tools/spec.md` sub-cluster 3 requirement "Approval
    Token Contract": the stub verifier MUST raise
    :class:`AutonomousMutationRejected` with the **literal** message
    ``"autonomous device mutation rejected: HITL approval token
    required"`` when the caller omits the approval token. The literal
    wording is the contract seam — slice 5 / Phase-3 audit tooling
    greps for it.
    """
    from nora.drivers.exceptions import AutonomousMutationRejected
    from nora.hitl.tokens import verify_approval_token

    expected = "autonomous device mutation rejected: HITL approval token required"

    with pytest.raises(AutonomousMutationRejected) as exc_info:
        verify_approval_token(None)

    assert str(exc_info.value) == expected, (
        f"AutonomousMutationRejected MUST carry the literal HITL message; "
        f"got {str(exc_info.value)!r}"
    )


# ---------------------------------------------------------------------------
# Named test #2 — migrate_requires_hitl_approval_token
# ---------------------------------------------------------------------------


def test_migrate_requires_hitl_approval_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """``verify_approval_token("expired-or-bogus")`` raises the same typed exception.

    The HITL gate is fail-closed: ANY non-conforming token (empty
    string, non-JSON garbage, expired payload) raises
    :class:`AutonomousMutationRejected` with the **same** literal
    message. The test covers the empty-string AND the non-JSON paths
    to prove the seam does not leak the rejection reason.
    """
    from nora.drivers.exceptions import AutonomousMutationRejected
    from nora.hitl.tokens import verify_approval_token

    expected = "autonomous device mutation rejected: HITL approval token required"

    # Path 1: empty string.
    with pytest.raises(AutonomousMutationRejected) as exc_info:
        verify_approval_token("")
    assert str(exc_info.value) == expected

    # Path 2: non-JSON garbage.
    with pytest.raises(AutonomousMutationRejected) as exc_info:
        verify_approval_token("expired-or-bogus")
    assert str(exc_info.value) == expected

    # Path 3: JSON payload but missing required field.
    with pytest.raises(AutonomousMutationRejected) as exc_info:
        verify_approval_token(json.dumps({"operator_id": "x", "issued_at": "now"}))
    assert str(exc_info.value) == expected

    # Side-effect check: no Settings mutation. The test asserts via the
    # public exception surface only; the verifier is a pure function
    # that does not touch ``Settings``.
    monkeypatch.delenv("NORA_HITL_TOKEN_TTL_SECONDS", raising=False)
    with pytest.raises(AutonomousMutationRejected):
        verify_approval_token(None)


# ---------------------------------------------------------------------------
# Named test #3 — hitl_mint_and_verify_round_trip
# ---------------------------------------------------------------------------


def test_hitl_mint_and_verify_round_trip() -> None:
    """A fresh token mints and verifies without raising.

    Round-trip contract: ``mint_token(...)`` returns a typed
    :class:`HitlApprovalToken` whose payload, re-encoded through
    :func:`verify_approval_token`, parses back into the same token
    identity. The test pins the operator id and ttl seconds so the
    mint+verify seam stays observable.
    """
    from pydantic import SecretStr

    from nora.hitl.tokens import HitlApprovalToken, mint_token, verify_approval_token

    signing_key = SecretStr("legacy-roundtrip-key")
    token_obj = mint_token("operator-7400", ttl_seconds=900, signing_key=signing_key)
    assert isinstance(token_obj, HitlApprovalToken)
    assert token_obj.operator_id == "operator-7400"

    # Encode the token for the verifier.
    wire_payload = token_obj.model_dump(mode="json")
    encoded = json.dumps(wire_payload)

    verified = verify_approval_token(encoded, signing_key=signing_key)
    assert verified.operator_id == "operator-7400"
    assert verified.token == token_obj.token


# ---------------------------------------------------------------------------
# Named test #4 — hitl_kill_switch_rejects_via_env_var
# ---------------------------------------------------------------------------


def test_hitl_kill_switch_rejects_via_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """``NORA_HITL_TOKEN_TTL_SECONDS=0`` makes the stub reject every token.

    Operational backout (per `tasks.md` PR 4 phase 4.1 backout note):
    setting the env var to ``0`` disables HITL acceptance even for
    well-formed, unexpired tokens. The kill switch is read directly
    from the process environment — NOT through :class:`Settings` —
    so an operator can disable the gate without rotating config.
    """
    from pydantic import SecretStr

    from nora.drivers.exceptions import AutonomousMutationRejected
    from nora.hitl.tokens import mint_token, verify_approval_token

    expected = "autonomous device mutation rejected: HITL approval token required"
    signing_key = SecretStr("kill-switch-key")

    monkeypatch.setenv("NORA_HITL_TOKEN_TTL_SECONDS", "0")
    try:
        token_obj = mint_token("operator-7400", ttl_seconds=900, signing_key=signing_key)
        encoded = json.dumps(token_obj.model_dump(mode="json"))
        with pytest.raises(AutonomousMutationRejected) as exc_info:
            verify_approval_token(encoded, signing_key=signing_key)
        assert str(exc_info.value) == expected
    finally:
        monkeypatch.delenv("NORA_HITL_TOKEN_TTL_SECONDS", raising=False)


# ---------------------------------------------------------------------------
# Defensive coverage — expired token + non-JSON payload + missing field.
# ---------------------------------------------------------------------------


def test_verify_rejects_expired_token() -> None:
    """An expired token (expires_at in the past) raises the typed exception.

    Even with a kill-switch-disengaged environment, the verifier MUST
    fail closed on expired tokens.
    """
    from nora.drivers.exceptions import AutonomousMutationRejected
    from nora.hitl.tokens import verify_approval_token

    expected = "autonomous device mutation rejected: HITL approval token required"
    expired = _encode_token(expires_at=datetime.now(timezone.utc) - timedelta(seconds=10))
    with pytest.raises(AutonomousMutationRejected) as exc_info:
        verify_approval_token(expired)
    assert str(exc_info.value) == expected


def test_verify_rejects_malformed_json() -> None:
    """A non-dict JSON payload (e.g. a list) raises the typed exception.

    Guards against a future refactor that loosens the ``isinstance(parsed,
    dict)`` guard.
    """
    from nora.drivers.exceptions import AutonomousMutationRejected
    from nora.hitl.tokens import verify_approval_token

    expected = "autonomous device mutation rejected: HITL approval token required"
    with pytest.raises(AutonomousMutationRejected) as exc_info:
        verify_approval_token(json.dumps([1, 2, 3]))
    assert str(exc_info.value) == expected


# ---------------------------------------------------------------------------
# Issue #43 / `2026-09-15-3tier-tool-governance` — HMAC-SHA256 mint/verify.
#
# Closes the forgeability gap surfaced in the issue body:
#   - `HitlApprovalToken` gains a non-empty `signature` field.
#   - `mint_token` computes HMAC-SHA256 over the canonical payload
#     `f"{operator_id}|{issued_at_iso8601}|{expires_at_iso8601}|{token}"`
#     with `Settings.nora_hitl_signing_key` as the key.
#   - `verify_approval_token` recomputes and compares via
#     `hmac.compare_digest` (NON-NEGOTIABLE — constant-time).
#   - Legacy stub tokens (no `signature`) raise
#     `AutonomousMutationRejected` with the literal message.
# ---------------------------------------------------------------------------


def test_mint_token_includes_nonempty_signature() -> None:
    """A freshly minted token carries a non-empty ``signature`` field.

    Per `openspec/changes/2026-09-15-3tier-tool-governance/specs/hitl-approval-tokens/spec.md`
    scenario "mint produces a typed model with a non-empty signature".
    """
    from pydantic import SecretStr

    from nora.hitl.tokens import HitlApprovalToken, mint_token

    signing_key = SecretStr("hermetic-key-do-not-leak")
    token_obj = mint_token("alice", ttl_seconds=900, signing_key=signing_key)

    assert isinstance(token_obj, HitlApprovalToken)
    assert token_obj.operator_id == "alice"
    assert isinstance(token_obj.signature, str)
    assert len(token_obj.signature) >= 1, "signature MUST be non-empty (Field min_length=1)"


def test_mint_and_verify_round_trip_with_hmac() -> None:
    """A token minted and verified with the same key passes HMAC verification.

    Per the same spec scenario "legitimate token verifies with compare_digest".
    """
    from pydantic import SecretStr

    from nora.hitl.tokens import mint_token, verify_approval_token

    signing_key = SecretStr("round-trip-key")
    token_obj = mint_token("alice", ttl_seconds=900, signing_key=signing_key)
    encoded = json.dumps(token_obj.model_dump(mode="json"))

    verified = verify_approval_token(encoded, signing_key=signing_key)

    assert verified.operator_id == "alice"
    assert verified.token == token_obj.token
    assert verified.signature == token_obj.signature


def test_verify_rejects_tampered_operator_id() -> None:
    """A token whose ``operator_id`` is rewritten after mint fails verification.

    Per the same spec scenario "mutated operator_id fails verification".
    The literal exception message MUST be preserved.
    """
    from pydantic import SecretStr

    from nora.drivers.exceptions import AutonomousMutationRejected
    from nora.hitl.tokens import mint_token, verify_approval_token

    signing_key = SecretStr("tamper-key")
    token_obj = mint_token("alice", ttl_seconds=900, signing_key=signing_key)
    payload = token_obj.model_dump(mode="json")
    payload["operator_id"] = "mallory"
    encoded = json.dumps(payload)

    expected = "autonomous device mutation rejected: HITL approval token required"
    with pytest.raises(AutonomousMutationRejected) as exc_info:
        verify_approval_token(encoded, signing_key=signing_key)
    assert str(exc_info.value) == expected


def test_verify_rejects_tampered_expires_at() -> None:
    """A token whose ``expires_at`` is rewritten after mint fails verification.

    Per the same spec scenario "mutated expires_at fails verification".
    """
    from datetime import datetime, timedelta, timezone

    from pydantic import SecretStr

    from nora.drivers.exceptions import AutonomousMutationRejected
    from nora.hitl.tokens import mint_token, verify_approval_token

    signing_key = SecretStr("tamper-key")
    token_obj = mint_token("alice", ttl_seconds=900, signing_key=signing_key)
    payload = token_obj.model_dump(mode="json")
    payload["expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=9999)).isoformat()
    encoded = json.dumps(payload)

    expected = "autonomous device mutation rejected: HITL approval token required"
    with pytest.raises(AutonomousMutationRejected) as exc_info:
        verify_approval_token(encoded, signing_key=signing_key)
    assert str(exc_info.value) == expected


def test_verify_rejects_wrong_signing_key() -> None:
    """A token signed under key K1 fails verification under key K2."""
    from pydantic import SecretStr

    from nora.drivers.exceptions import AutonomousMutationRejected
    from nora.hitl.tokens import mint_token, verify_approval_token

    token_obj = mint_token("alice", ttl_seconds=900, signing_key=SecretStr("K1"))
    encoded = json.dumps(token_obj.model_dump(mode="json"))

    expected = "autonomous device mutation rejected: HITL approval token required"
    with pytest.raises(AutonomousMutationRejected) as exc_info:
        verify_approval_token(encoded, signing_key=SecretStr("K2"))
    assert str(exc_info.value) == expected


def test_verify_rejects_legacy_stub_token_without_signature() -> None:
    """A legacy stub payload (no ``signature`` key) fails Pydantic validation.

    Per the same spec scenario "stub token without signature fails verification".
    The schema requires ``signature: str = Field(min_length=1)``; legacy
    payloads missing the field fail ``model_validate`` →
    ``AutonomousMutationRejected`` with the literal message.
    """
    from pydantic import SecretStr

    from nora.drivers.exceptions import AutonomousMutationRejected
    from nora.hitl.tokens import verify_approval_token

    payload = {
        "token": "stub-alice-1234",
        "operator_id": "alice",
        "issued_at": "2026-09-15T00:00:00+00:00",
        "expires_at": "2026-09-15T01:00:00+00:00",
    }
    encoded = json.dumps(payload)
    expected = "autonomous device mutation rejected: HITL approval token required"
    with pytest.raises(AutonomousMutationRejected) as exc_info:
        verify_approval_token(encoded, signing_key=SecretStr("any-key"))
    assert str(exc_info.value) == expected


def test_verify_rejects_empty_signature() -> None:
    """A payload with an empty ``signature`` fails Field(min_length=1)."""
    from pydantic import SecretStr

    from nora.drivers.exceptions import AutonomousMutationRejected
    from nora.hitl.tokens import verify_approval_token

    payload = {
        "token": "stub-alice-1234",
        "operator_id": "alice",
        "issued_at": "2026-09-15T00:00:00+00:00",
        "expires_at": "2026-09-15T01:00:00+00:00",
        "signature": "",
    }
    encoded = json.dumps(payload)
    expected = "autonomous device mutation rejected: HITL approval token required"
    with pytest.raises(AutonomousMutationRejected) as exc_info:
        verify_approval_token(encoded, signing_key=SecretStr("any-key"))
    assert str(exc_info.value) == expected


def test_verify_uses_hmac_compare_digest_for_timing_attack_resistance() -> None:
    """``verify_approval_token`` source uses ``hmac.compare_digest`` (no ``==`` on signatures).

    Per the same spec scenario "verifier uses compare_digest (timing-attack
    resistance)". Static AST scan: the function MUST reference
    ``hmac.compare_digest`` AND MUST NOT compare signatures with ``==``.
    """
    import ast
    from pathlib import Path

    src_path = Path(__file__).resolve().parent.parent / "src" / "nora" / "hitl" / "tokens.py"
    tree = ast.parse(src_path.read_text())

    verify_fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "verify_approval_token"
    )
    has_compare_digest = False
    has_equality_on_signature = False
    for sub in ast.walk(verify_fn):
        if isinstance(sub, ast.Call):
            func = sub.func
            if isinstance(func, ast.Attribute) and func.attr == "compare_digest":
                has_compare_digest = True
        # Catch bare `signature == <something>` comparisons inside the verifier
        if isinstance(sub, ast.Compare):
            for comparator in sub.comparators:
                if isinstance(comparator, ast.Name) and comparator.id == "signature":
                    has_equality_on_signature = True
            for left in sub.left if isinstance(sub.left, list) else [sub.left]:
                if isinstance(left, ast.Name) and left.id == "signature":
                    has_equality_on_signature = True

    assert has_compare_digest, (
        "verify_approval_token must call hmac.compare_digest() for timing-attack resistance"
    )
    assert not has_equality_on_signature, (
        "verify_approval_token must NOT compare signatures with `==` "
        "(use hmac.compare_digest for constant-time)"
    )


def test_verify_fails_closed_when_signing_key_is_none() -> None:
    """A ``None`` signing key on Tier-2 invocation raises the typed exception.

    Per `secure-configuration` scenario "missing HITL signing key fails
    closed on Tier-2 invocation" — lazy fail-closed at first mint.
    """
    from nora.drivers.exceptions import AutonomousMutationRejected
    from nora.hitl.tokens import mint_token, verify_approval_token

    expected = "autonomous device mutation rejected: HITL approval token required"
    with pytest.raises(AutonomousMutationRejected) as exc_info:
        mint_token("alice", ttl_seconds=900, signing_key=None)
    assert str(exc_info.value) == expected

    with pytest.raises(AutonomousMutationRejected) as exc_info:
        verify_approval_token("not-a-real-token", signing_key=None)
    assert str(exc_info.value) == expected


def test_canonical_payload_format_is_frozen_pipe_joined_tuple() -> None:
    """The canonical signing payload is the frozen tuple format.

    Per `design.md` ADR-6: ``f"{operator_id}|{issued_at}|{expires_at}|{token}"``.
    NOT a JSON dict — eliminates canonicalization drift. The test
    reconstructs the canonical payload from the model and asserts the
    exact byte-level format including the ``|`` separator.
    """
    from pydantic import SecretStr

    from nora.hitl.tokens import mint_token

    signing_key = SecretStr("canonical-key")
    token_obj = mint_token("alice", ttl_seconds=900, signing_key=signing_key)
    canonical = (
        f"{token_obj.operator_id}|"
        f"{token_obj.issued_at.isoformat()}|"
        f"{token_obj.expires_at.isoformat()}|"
        f"{token_obj.token}"
    )
    # Four pipe-separated fields, no JSON braces, no spaces.
    assert canonical.count("|") == 3
    assert "{" not in canonical
    assert "}" not in canonical
    assert "operator_id" not in canonical  # NOT a JSON dict
    # Round-trip property: same canonical payload, same HMAC.
    import hashlib
    import hmac

    expected_sig = hmac.new(
        signing_key.get_secret_value().encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert token_obj.signature == expected_sig


def test_no_dual_verify_window_for_legacy_stub_tokens() -> None:
    """Legacy stub tokens (no signature) FAIL on the new verifier — hard-break.

    Per ADR-2: hard-break default. Pre-deploy stub tokens are
    invalidated at deploy time; there is NO dual-verify window that
    would re-open the forgeability gap.
    """
    from pydantic import SecretStr

    from nora.drivers.exceptions import AutonomousMutationRejected
    from nora.hitl.tokens import verify_approval_token

    legacy = json.dumps(
        {
            "token": "stub-alice-9999",
            "operator_id": "alice",
            "issued_at": "2026-09-15T00:00:00+00:00",
            "expires_at": "2099-12-31T23:59:59+00:00",  # far in the future
        }
    )
    expected = "autonomous device mutation rejected: HITL approval token required"
    with pytest.raises(AutonomousMutationRejected) as exc_info:
        verify_approval_token(legacy, signing_key=SecretStr("any"))
    assert str(exc_info.value) == expected


__all__ = [
    "test_migrate_autonomous_call_raises_autonomous_mutation_rejected",
    "test_migrate_requires_hitl_approval_token",
    "test_hitl_mint_and_verify_round_trip",
    "test_hitl_kill_switch_rejects_via_env_var",
    "test_verify_rejects_expired_token",
    "test_verify_rejects_malformed_json",
    # Issue #43 / HMAC
    "test_mint_token_includes_nonempty_signature",
    "test_mint_and_verify_round_trip_with_hmac",
    "test_verify_rejects_tampered_operator_id",
    "test_verify_rejects_tampered_expires_at",
    "test_verify_rejects_wrong_signing_key",
    "test_verify_rejects_legacy_stub_token_without_signature",
    "test_verify_rejects_empty_signature",
    "test_verify_uses_hmac_compare_digest_for_timing_attack_resistance",
    "test_verify_fails_closed_when_signing_key_is_none",
    "test_canonical_payload_format_is_frozen_pipe_joined_tuple",
    "test_no_dual_verify_window_for_legacy_stub_tokens",
]
