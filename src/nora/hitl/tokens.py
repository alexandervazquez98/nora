"""HITL approval token — HMAC-SHA256 mint/verify, forgeability closure.

The "human-in-the-loop" gate for Tier-2 mutations (e.g. RF migration,
intervention record writes from autonomous contexts) lives at
``nora.hitl.tokens``. The Tier-2 mutation path calls
:func:`verify_approval_token` BEFORE any state-changing wire frame is
emitted; missing or invalid tokens raise
:class:`AutonomousMutationRejected` with the **literal** message pinned
in ``tests/test_hitl_tokens.py::migrate_autonomous_call_raises_autonomous_mutation_rejected``.

Scope:

* HMAC-SHA256 mint/verify over the canonical tuple payload
  ``f"{operator_id}|{issued_at_iso8601}|{expires_at_iso8601}|{token}"``
  (frozen per `design.md` ADR-6; non-negotiable).
* Constant-time comparison via :func:`hmac.compare_digest` (frozen per
  ADR-6; prevents timing oracles against the operator's signing key).
* Signing key sourced from ``Settings.nora_hitl_signing_key: SecretStr``;
  lazy fail-closed when ``None`` (Tier-2 invocation raises the typed
  exception; Tier-0/Tier-1 boot proceeds).
* Legacy stub tokens (no ``signature`` field) FAIL Pydantic validation
  → :class:`AutonomousMutationRejected` with the literal message. NO
  dual-verify window — the forgeability gap is closed.

The literal ``_REJECTED_MESSAGE`` string is the contract seam; the
spec ``pmp450i-radio-tools/spec.md`` sub-cluster 3 requirement "Approval
Token Contract" pins the wording verbatim.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, SecretStr

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
    a non-empty ``token`` (the opaque identifier emitted by
    ``mint_token``), the issuing operator id, ``issued_at`` /
    ``expires_at`` timestamps used to reject expired tokens, and a
    non-empty ``signature`` (HMAC-SHA256 hex digest) computed over the
    canonical payload tuple.

    The model is :class:`frozen=True` so a token cannot be mutated
    between mint and verify. The ``signature`` field is required by the
    schema (Pydantic rejects legacy stub payloads missing it — closing
    the forgeability gap surfaced in issue #43).
    """

    model_config = ConfigDict(frozen=True)

    token: str = Field(min_length=1)
    operator_id: str = Field(min_length=1)
    issued_at: datetime
    expires_at: datetime
    signature: str = Field(min_length=1)


def _canonical_payload(
    *,
    operator_id: str,
    issued_at: datetime,
    expires_at: datetime,
    token: str,
) -> bytes:
    """Build the canonical signing payload (frozen tuple, UTF-8 encoded).

    Per `design.md` ADR-6: ``f"{operator_id}|{issued_at_iso8601}|{expires_at_iso8601}|{token}"``.
    NOT a JSON dict — eliminates canonicalization drift (key ordering,
    whitespace, separator drift).
    """
    return (f"{operator_id}|{issued_at.isoformat()}|{expires_at.isoformat()}|{token}").encode(
        "utf-8"
    )


def _coerce_key_bytes(signing_key: SecretStr | str | None) -> bytes:
    """Coerce a signing-key source to bytes; raise if empty (lazy fail-closed)."""
    if signing_key is None:
        raise AutonomousMutationRejected(_REJECTED_MESSAGE)
    if isinstance(signing_key, SecretStr):
        raw = signing_key.get_secret_value()
    else:
        raw = str(signing_key)
    if not raw:
        raise AutonomousMutationRejected(_REJECTED_MESSAGE)
    return raw.encode("utf-8")


def mint_token(
    operator_id: str,
    *,
    ttl_seconds: int = 900,
    signing_key: SecretStr | str | None = None,
) -> HitlApprovalToken:
    """Mint a HMAC-signed approval token with a fresh ``issued_at``.

    The ``signing_key`` MUST come from ``Settings.nora_hitl_signing_key``
    in production. Tests inject a hermetic ``SecretStr`` literal.
    ``None`` or empty keys raise :class:`AutonomousMutationRejected`
    (lazy fail-closed — Tier-0/Tier-1 boot proceeds; Tier-2 invocation
    raises).

    Args:
        operator_id: The operator id bound to the token. Required
            (``min_length=1``); empty ids raise ``ValueError``.
        ttl_seconds: Token time-to-live in seconds. The
            ``expires_at`` field is ``issued_at + ttl_seconds``.
            Defaults to 900 (15 minutes) — matches
            ``Settings.nora_hitl_token_ttl_seconds``.
        signing_key: HMAC signing key (``SecretStr`` or plain ``str``).

    Returns:
        A typed :class:`HitlApprovalToken` carrying a non-empty
        ``signature`` ready for :func:`verify_approval_token`.

    Raises:
        AutonomousMutationRejected: empty / missing signing key.
    """
    key_bytes = _coerce_key_bytes(signing_key)

    now = datetime.now(timezone.utc)
    token = f"hitl-{operator_id}-{int(now.timestamp())}"
    payload = _canonical_payload(
        operator_id=operator_id,
        issued_at=now,
        expires_at=now + timedelta(seconds=ttl_seconds),
        token=token,
    )
    signature = hmac.new(key_bytes, payload, hashlib.sha256).hexdigest()

    return HitlApprovalToken(
        token=token,
        operator_id=operator_id,
        issued_at=now,
        expires_at=now + timedelta(seconds=ttl_seconds),
        signature=signature,
    )


def verify_approval_token(
    token: str | None,
    *,
    signing_key: SecretStr | str | None = None,
) -> HitlApprovalToken:
    """Verify ``token`` and return the parsed :class:`HitlApprovalToken`.

    The HMAC verifier accepts a JSON-encoded payload of the form::

        {
            "token": "<id>",
            "operator_id": "<op>",
            "issued_at": "...",
            "expires_at": "...",
            "signature": "<hmac_sha256_hex>",
        }

    The canonical payload tuple is recomputed from the parsed model and
    the signature is compared via :func:`hmac.compare_digest`
    (NON-NEGOTIABLE — constant-time; prevents timing oracles against
    the operator's signing key per `design.md` ADR-6).

    Missing, empty, syntactically-bogus, expired, kill-switched, or
    signature-mismatched tokens raise
    :class:`AutonomousMutationRejected` with the **literal** message
    pinned in :data:`_REJECTED_MESSAGE`. The exception fires before any
    caller can emit an SNMP SET frame.

    Args:
        token: The opaque token string handed to the migration tool.
            May be ``None``, empty, or any non-JSON payload; every
            non-conforming path raises the typed exception.
        signing_key: HMAC signing key (``SecretStr`` or plain ``str``).

    Returns:
        The parsed :class:`HitlApprovalToken` on success.

    Raises:
        AutonomousMutationRejected: missing, empty, invalid, expired,
            kill-switched, or signature-mismatched tokens. The
            exception's ``str(...)`` is the canonical contract seam —
            pinned verbatim in
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

    # HMAC verification — recompute the canonical payload and compare
    # via ``hmac.compare_digest`` (constant-time). Lazy fail-closed if
    # signing key is empty.
    try:
        key_bytes = _coerce_key_bytes(signing_key)
    except AutonomousMutationRejected:
        raise

    payload = _canonical_payload(
        operator_id=verified.operator_id,
        issued_at=verified.issued_at,
        expires_at=verified.expires_at,
        token=verified.token,
    )
    expected_signature = hmac.new(key_bytes, payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_signature, verified.signature):
        raise AutonomousMutationRejected(_REJECTED_MESSAGE)

    return verified


__all__ = [
    "AutonomousMutationRejected",
    "HitlApprovalToken",
    "mint_token",
    "verify_approval_token",
]
