"""Pydantic models — durable on-disk contract for the session journal.

These models are PURE DATA: no I/O, no sanitizer, no clock. They live in
their own module so Pydantic validation is testable in isolation and so
`session_journal.py` can stay focused on persistence + API.

The frozen `Outcome` literal mirrors the R2 enum: exactly 3 values, no
others are accepted.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator
from typing_extensions import TypeAlias

# R2 — `outcome` is one of three literals. Module-level alias so callers
# can `from nora.core.session_models import Outcome` and use it for
# annotations.
Outcome: TypeAlias = Literal["success", "error", "timeout"]
__all__ = ["Outcome", "SessionState", "SessionStep"]


class SessionStep(BaseModel):
    """One row of the per-tool-call trace. Append-only on disk.

    `ts` is captured by the middleware BEFORE the tool body runs (R2).
    `step` is monotonic per-session and starts at 1 (R2).
    `input` may contain keys on the R10 frozen list — those values are
    masked by the redaction walker before this model is constructed.
    """

    ts: datetime
    step: int = Field(ge=1)
    tool: str
    input: dict[str, Any]
    result_summary: str
    duration_ms: int = Field(ge=0)
    outcome: Outcome
    llm_interpretation: str | None = None


class SessionState(BaseModel):
    """Canonical on-disk shape for one investigation session (R1, R9, R13).

    `session_id` is UUIDv4 at construction and MUST NOT change afterward;
    `SessionJournal` relies on this for `resume()` semantics. `operator_alias`
    is bounded to 64 chars per R13; longer values fail validation, never
    silently truncate.
    """

    session_id: str
    operator_alias: str = Field(max_length=64)
    started_at: datetime
    focus_device_id: str | None = None
    devices_reviewed: list[str] = Field(default_factory=list)
    trace: list[SessionStep] = Field(default_factory=list)
    last_updated: datetime | None = None

    @model_validator(mode="after")
    def _default_last_updated(self) -> "SessionState":
        """R1 — `last_updated` equals `started_at` at creation."""
        if self.last_updated is None:
            self.last_updated = self.started_at
        return self
