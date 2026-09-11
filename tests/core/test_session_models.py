"""SessionState / SessionStep Pydantic models — R1, R2, R9, R13.

These models are the durable on-disk contract for the journal. They have
NO I/O and NO sanitizer coupling — that's why they live in their own
module. Every later commit depends on these shapes, so they MUST be the
first production code in the package (after the REDACTION_LIST).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# R1 — SessionState field set on creation
# ---------------------------------------------------------------------------


def test_session_state_creation_has_all_required_keys() -> None:
    """A new `SessionState` carries the 7 required keys (R1)."""
    from nora.core.session_models import SessionState

    state = SessionState(
        session_id=str(uuid.uuid4()),
        operator_alias="anon",
        started_at=_now(),
    )

    assert set(state.model_dump().keys()) >= {
        "session_id",
        "operator_alias",
        "started_at",
        "focus_device_id",
        "devices_reviewed",
        "trace",
        "last_updated",
    }
    assert state.focus_device_id is None
    assert state.devices_reviewed == []
    assert state.trace == []


def test_session_state_session_id_is_uuidv4_string() -> None:
    """R1 — `session_id` parses as UUIDv4 on a fresh SessionState."""
    from nora.core.session_models import SessionState

    state = SessionState(
        session_id=str(uuid.uuid4()),
        operator_alias="anon",
        started_at=_now(),
    )
    parsed = uuid.UUID(state.session_id)
    assert parsed.version == 4, f"session_id is not UUIDv4: {state.session_id!r}"


# ---------------------------------------------------------------------------
# R2 — SessionStep field set
# ---------------------------------------------------------------------------


def test_session_step_carries_seven_required_fields() -> None:
    """A new `SessionStep` carries all 7 required fields with valid types."""
    from nora.core.session_models import SessionStep

    step = SessionStep(
        ts=_now(),
        step=1,
        tool="nora_health",
        input={"device_id": "ap-7400-01"},
        result_summary="ok",
        duration_ms=12,
        outcome="success",
    )

    assert step.ts.tzinfo is not None, "ts MUST be timezone-aware"
    assert step.step == 1
    assert step.tool == "nora_health"
    assert step.input == {"device_id": "ap-7400-01"}
    assert step.result_summary == "ok"
    assert step.duration_ms == 12
    assert step.outcome == "success"
    assert step.llm_interpretation is None, "llm_interpretation defaults to None"


@pytest.mark.parametrize("outcome_value", ["success", "error", "timeout"])
def test_session_step_outcome_accepts_literal_values(outcome_value: str) -> None:
    """`outcome` MUST accept the 3 literal values (R2 boundary)."""
    from nora.core.session_models import SessionStep

    step = SessionStep(
        ts=_now(),
        step=1,
        tool="nora_health",
        input={},
        result_summary="ok",
        duration_ms=0,
        outcome=outcome_value,  # type: ignore[arg-type]
    )
    assert step.outcome == outcome_value


def test_session_step_outcome_rejects_unknown_literal() -> None:
    """`outcome="warn"` MUST raise ValidationError — only the 3 literals are valid."""
    from nora.core.session_models import SessionStep

    with pytest.raises(ValidationError):
        SessionStep(
            ts=_now(),
            step=1,
            tool="nora_health",
            input={},
            result_summary="ok",
            duration_ms=0,
            outcome="warn",  # type: ignore[arg-type]
        )


def test_session_step_duration_ms_must_be_non_negative() -> None:
    """`duration_ms` MUST be ≥ 0."""
    from nora.core.session_models import SessionStep

    with pytest.raises(ValidationError):
        SessionStep(
            ts=_now(),
            step=1,
            tool="nora_health",
            input={},
            result_summary="ok",
            duration_ms=-1,
            outcome="success",
        )


# ---------------------------------------------------------------------------
# R9 — session_id initial state
# ---------------------------------------------------------------------------


def test_session_state_session_id_is_immutable_on_construction() -> None:
    """R9 — `session_id` is set once at construction; reading it twice is stable."""
    from nora.core.session_models import SessionState

    sid = str(uuid.uuid4())
    state = SessionState(session_id=sid, operator_alias="anon", started_at=_now())
    assert state.session_id == sid
    # Re-reading after another mutation still yields the same id.
    state.devices_reviewed.append("ap-7400-01")
    assert state.session_id == sid


# ---------------------------------------------------------------------------
# R13 — operator_alias max_length = 64
# ---------------------------------------------------------------------------


def test_session_state_operator_alias_accepts_64_chars() -> None:
    """A 64-char `operator_alias` is accepted (R13 boundary)."""
    from nora.core.session_models import SessionState

    state = SessionState(
        session_id=str(uuid.uuid4()),
        operator_alias="a" * 64,
        started_at=_now(),
    )
    assert len(state.operator_alias) == 64


def test_session_state_operator_alias_rejects_65_chars() -> None:
    """A 65-char `operator_alias` MUST raise ValidationError (R13)."""
    from nora.core.session_models import SessionState

    with pytest.raises(ValidationError):
        SessionState(
            session_id=str(uuid.uuid4()),
            operator_alias="a" * 65,
            started_at=_now(),
        )


# ---------------------------------------------------------------------------
# Typed exception hierarchy (R7, R11, R15)
# ---------------------------------------------------------------------------


def test_session_journal_error_is_the_base() -> None:
    """All journal exceptions MUST inherit from `SessionJournalError`."""
    from nora.core.session_journal import (
        JournalCorruptError,
        JournalDisabledError,
        SessionJournalError,
        SessionNotFoundError,
    )

    for cls in (SessionNotFoundError, JournalCorruptError, JournalDisabledError):
        assert issubclass(cls, SessionJournalError), (
            f"{cls.__name__} must inherit SessionJournalError"
        )


def test_session_journal_error_is_an_exception() -> None:
    """`SessionJournalError` MUST be an `Exception` subclass (catchable)."""
    from nora.core.session_journal import SessionJournalError

    assert issubclass(SessionJournalError, Exception)
