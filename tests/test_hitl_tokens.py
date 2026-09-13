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
    from nora.hitl.tokens import HitlApprovalToken, mint_token, verify_approval_token

    token_obj = mint_token("operator-7400", ttl_seconds=900)
    assert isinstance(token_obj, HitlApprovalToken)
    assert token_obj.operator_id == "operator-7400"

    # Encode the token for the verifier.
    wire_payload = token_obj.model_dump(mode="json")
    encoded = json.dumps(wire_payload)

    verified = verify_approval_token(encoded)
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
    from nora.drivers.exceptions import AutonomousMutationRejected
    from nora.hitl.tokens import mint_token, verify_approval_token

    expected = "autonomous device mutation rejected: HITL approval token required"

    monkeypatch.setenv("NORA_HITL_TOKEN_TTL_SECONDS", "0")
    try:
        token_obj = mint_token("operator-7400", ttl_seconds=900)
        encoded = json.dumps(token_obj.model_dump(mode="json"))
        with pytest.raises(AutonomousMutationRejected) as exc_info:
            verify_approval_token(encoded)
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


__all__ = [
    "test_migrate_autonomous_call_raises_autonomous_mutation_rejected",
    "test_migrate_requires_hitl_approval_token",
    "test_hitl_mint_and_verify_round_trip",
    "test_hitl_kill_switch_rejects_via_env_var",
    "test_verify_rejects_expired_token",
    "test_verify_rejects_malformed_json",
]
