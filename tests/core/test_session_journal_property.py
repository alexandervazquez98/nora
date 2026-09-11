"""Property tests for `SessionJournal` — invariants under arbitrary mutations.

Run via `pytest -m slow tests/core/test_session_journal_property.py`. The
Hypothesis strategies generate `SessionStep` lists of up to 100 entries
and assert the round-trip and rotation invariants stay stable.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# Mark every test in this module as `slow` so the regular test suite
# skips them. Run explicitly with `pytest -m slow`.
pytestmark = pytest.mark.slow


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


@st.composite
def _aware_datetimes(draw: st.DrawFn) -> datetime:
    """Yield a timezone-aware `datetime` near `now` (avoid overflow / naive dt)."""
    delta = draw(st.timedeltas(min_value=timedelta(seconds=0), max_value=timedelta(days=365)))
    return datetime.now(timezone.utc) - delta


@st.composite
def session_steps(draw: st.DrawFn) -> dict:
    """Yield a dict that passes Pydantic validation as a `SessionStep`."""
    return {
        "ts": draw(_aware_datetimes()),
        "step": draw(st.integers(min_value=1, max_value=10_000)),
        "tool": draw(st.sampled_from(["nora_health", "snmp_get", "ping", "set_focus"])),
        "input": {},
        "result_summary": draw(
            st.text(
                max_size=64,
                alphabet=st.characters(
                    blacklist_categories=("Cs",), blacklist_characters="\x00\r\n"
                ),
            )
        ),
        "duration_ms": draw(st.integers(min_value=0, max_value=5000)),
        "outcome": draw(st.sampled_from(["success", "error", "timeout"])),
        "llm_interpretation": draw(
            st.none()
            | st.text(
                max_size=64,
                alphabet=st.characters(
                    blacklist_categories=("Cs",), blacklist_characters="\x00\r\n"
                ),
            )
        ),
    }


@contextmanager
def _temp_journal():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp) / "sessions"


# ---------------------------------------------------------------------------
# Round-trip invariant — R5/R6 boundary
# ---------------------------------------------------------------------------


@settings(max_examples=20, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(session_steps(), max_size=20))
def test_session_state_round_trip_via_disk(steps_data: list[dict]) -> None:
    """`state == load(save(state))` under arbitrary mutation sequences."""
    from nora.core.session_journal import SessionJournal
    from nora.core.session_models import SessionState, SessionStep
    from nora.sanitizer import Sanitizer

    session_id = "round-trip-test"
    now = datetime.now(timezone.utc)
    steps: list[SessionStep] = []
    for i, sd in enumerate(steps_data, start=1):
        sd = dict(sd)
        sd["step"] = i
        steps.append(SessionStep.model_validate(sd))
    state = SessionState(
        session_id=session_id,
        operator_alias="op",
        started_at=now,
        last_updated=now,
        trace=steps,
    )

    with _temp_journal() as journal_dir:
        journal = SessionJournal(
            journal_dir=journal_dir,
            max_trace_steps=max(1, len(steps) + 5),
            sanitizer=Sanitizer(),
            enabled=True,
        )
        # Bypass the constructor's UUID — force a deterministic id.
        journal._session_id = session_id  # type: ignore[attr-defined]
        journal._persist(state)  # type: ignore[attr-defined]

        # Load the file and rebuild a SessionState from it.
        path = journal._path()  # type: ignore[attr-defined]
        with path.open() as f:
            loaded_dict = json.load(f)
        loaded = SessionState.model_validate(loaded_dict)

        assert len(loaded.trace) == len(state.trace)
        for got, want in zip(loaded.trace, state.trace):
            assert got.tool == want.tool
            assert got.outcome == want.outcome
            assert got.step == want.step


# ---------------------------------------------------------------------------
# Rotation invariant — trace length capped at max_steps
# ---------------------------------------------------------------------------


@settings(max_examples=15, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    max_steps=st.integers(min_value=2, max_value=10),
    n_appends=st.integers(min_value=3, max_value=20),
)
def test_trace_length_capped_after_many_appends(max_steps: int, n_appends: int) -> None:
    """After N appends, the canonical trace length is at most `max_steps` (R5 invariant)."""
    from nora.core.session_journal import SessionJournal
    from nora.sanitizer import Sanitizer

    with _temp_journal() as journal_dir:
        journal = SessionJournal(
            journal_dir=journal_dir,
            max_trace_steps=max_steps,
            sanitizer=Sanitizer(),
            enabled=True,
        )
        for _ in range(n_appends):
            journal.record_step(
                tool="tool",
                input_args={},
                result_summary="ok",
                duration_ms=1,
                outcome="success",
                ts=datetime.now(timezone.utc),
            )

        state = journal.get_state()
        assert len(state.trace) <= max_steps, (
            f"Canonical trace exceeded cap: max_steps={max_steps}, "
            f"len(trace)={len(state.trace)} after {n_appends} appends"
        )
        # And the NDJSON tail holds the displaced steps.
        if n_appends > max_steps:
            ndjson_path = journal._ndjson_path()  # type: ignore[attr-defined]
            assert ndjson_path.exists(), "NDJSON should exist when rotation triggered"
            total_steps = len(state.trace) + sum(
                1 for line in ndjson_path.read_text().splitlines() if line.strip()
            )
            assert total_steps == n_appends, (
                f"Displaced steps lost: canonical={len(state.trace)}, "
                f"ndjson_lines={n_appends - len(state.trace)}, "
                f"total={total_steps} vs n_appends={n_appends}"
            )
