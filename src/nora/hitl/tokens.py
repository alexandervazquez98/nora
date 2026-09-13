"""HITL approval token + stub verifier — slice 4 (PR 4 commit 1).

The "human-in-the-loop" gate for the slice-4 RF migration tool is
implemented as a typed approval-token verifier at
``nora.hitl.tokens``. The migration tool (``snmp_migrate_radio_frequency``)
calls :func:`verify_approval_token` BEFORE any SNMP SET frame is
emitted; missing or invalid tokens raise :class:`AutonomousMutationRejected`
with the **literal** message pinned in
``tests/test_hitl_tokens.py::migrate_autonomous_call_raises_autonomous_mutation_rejected``.

Stub scope (per `design.md` architecture decision #1):

* The stub is a fail-closed verifier: any non-empty token that
  matches the schema is accepted; missing / empty / syntactically-bogus
  tokens are rejected.
* Full state machine (token minting via the operator UI, ticket
  binding, replay protection, audit-log emission) lands in the
  Phase-3 HITL ChangeRequest cluster — out of scope for slice 4.
* The literal ``AutonomousMutationRejected("autonomous device
  mutation rejected: HITL approval token required")`` message is the
  contract seam; slicing the change around that literal keeps the
  verifier call site testable without coupling to Pydantic / Settings.

Operational backout escape hatch (per `tasks.md` PR 4 phase 4.1
backout note): setting ``NORA_HITL_TOKEN_TTL_SECONDS=0`` makes the
stub reject every token. The TTL is read from the process
environment through :func:`nora.config.hitl_kill_switch_active` —
the helper is co-located with the ``Settings`` boundary so the
operator can rotate the kill-switch even when ``Settings`` is
locked or the process is reading from a pinned config.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from nora.config import hitl_kill_switch_active
from nora.drivers.exceptions import AutonomousMutationRejected

# Literal error message — MUST stay verbatim so the test
# ``migrate_autonomous_call_raises_autonomous_mutation_rejected``
# stays green across the chain. The spec
# ``pmp450i-radio-tools/spec.md`` sub-cluster 3 requirement "Approval
# Token Contract" pins the wording.
_REJECTED_MESSAGE: Final[str] = "autonomous device mutation rejected: HITL approval token required"


class HitlApprovalToken(BaseModel):
    """A single HITL approval token.

    The model is the contract seam: :func:`mint_token` produces one,
    :func:`verify_approval_token` consumes one. A valid token carries
    a non-empty ``token`` (the opaque identifier emitted by the
    operator UI in the Phase-3 cluster — slice 4 uses a stub
    :func:`mint_token` that ships a non-cryptographic placeholder),
    the issuing operator id, an ``issued_at`` timestamp, and an
    ``expires_at`` timestamp used by the stub verifier to reject
    expired tokens.

    The model is :class:`frozen=True` so a token cannot be mutated
    between mint and verify.
    """

    model_config = ConfigDict(frozen=True)

    token: str = Field(min_length=1)
    operator_id: str = Field(min_length=1)
    issued_at: datetime
    expires_at: datetime


def mint_token(operator_id: str, *, ttl_seconds: int = 900) -> HitlApprovalToken:
    """Mint a stub approval token with a fresh ``issued_at``.

    The Phase-3 cluster replaces this with a cryptographically-signed
    ticket binding (operator id, ticket number, request timestamp).
    Slice 4 only needs the verifier seam; the stub emits a unique
    ``token`` string keyed off the ``operator_id`` and the mint
    timestamp so the migration tests can mint and verify a token
    against the same handler without collision.

    Args:
        operator_id: The operator id bound to the token. Required
            (``min_length=1``); empty ids raise ``ValueError``.
        ttl_seconds: Token time-to-live in seconds. The
            ``expires_at`` field is ``issued_at + ttl_seconds``.
            Defaults to 900 (15 minutes) — matches
            ``Settings.nora_hitl_token_ttl_seconds``.

    Returns:
        A typed :class:`HitlApprovalToken` ready for
        :func:`verify_approval_token`.
    """
    now = datetime.now(timezone.utc)
    return HitlApprovalToken(
        token=f"stub-{operator_id}-{int(now.timestamp())}",
        operator_id=operator_id,
        issued_at=now,
        expires_at=now + timedelta(seconds=ttl_seconds),
    )


def verify_approval_token(token: str | None) -> HitlApprovalToken:
    """Verify ``token`` and return the parsed :class:`HitlApprovalToken`.

    The stub accepts a JSON-encoded payload of the form::

        {"token": "<id>", "operator_id": "<op>", "issued_at": "...", "expires_at": "..."}

    Missing, empty, syntactically-bogus, expired, or kill-switched
    tokens raise :class:`AutonomousMutationRejected` with the
    **literal** message pinned in :data:`_REJECTED_MESSAGE`. The
    exception fires before any caller can emit an SNMP SET frame.

    Args:
        token: The opaque token string handed to the migration
            tool. May be ``None``, empty, or any non-JSON payload;
            every non-conforming path raises the typed exception.

    Returns:
        The parsed :class:`HitlApprovalToken` on success.

    Raises:
        AutonomousMutationRejected: missing, empty, invalid,
            expired, or kill-switched tokens. The exception's
            ``str(...)`` is the canonical contract seam — pinned
            verbatim in
            ``tests/test_hitl_tokens.py::migrate_autonomous_call_raises_autonomous_mutation_rejected``.
    """
    if not token:
        raise AutonomousMutationRejected(_REJECTED_MESSAGE)

    if hitl_kill_switch_active():
        raise AutonomousMutationRejected(_REJECTED_MESSAGE)

    try:
        parsed = json.loads(token)
    except (TypeError, ValueError):
        raise AutonomousMutationRejected(_REJECTED_MESSAGE) from None

    if not isinstance(parsed, dict):
        raise AutonomousMutationRejected(_REJECTED_MESSAGE)

    try:
        verified = HitlApprovalToken.model_validate(parsed)
    except Exception:  # noqa: BLE001 — any parse/validation miss is a rejection
        raise AutonomousMutationRejected(_REJECTED_MESSAGE) from None

    now = datetime.now(timezone.utc)
    if verified.expires_at <= now:
        raise AutonomousMutationRejected(_REJECTED_MESSAGE)

    return verified


__all__ = [
    "HitlApprovalToken",
    "mint_token",
    "verify_approval_token",
]
