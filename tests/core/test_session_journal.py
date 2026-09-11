"""SessionJournal core — happy-path recording contract.

This file accumulates tests as features land:
    task #4 — R1, R2, R3, R7-S2, R9 (load / save / append / get_state)
    task #5 — R6, R10 (redaction + sanitizer)
    task #6 — R5 (NDJSON rotation)
    task #7 — R12 (4-tuple preserved)
    task #11 — R11, R18 (corrupt + POSIX mode)
    task #10 — R15 (disable switch)
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings(journal_dir: Path):
    """Build a Settings bound to `journal_dir`, no .env, no process-env."""
    from nora.config import Settings

    return Settings(
        _env_file=None,
        _env_file_encoding=None,
        nora_session_journal_dir=journal_dir,
        nora_session_trace_max_steps=50,
        nora_session_journal_enabled=True,
        nora_operator_alias="test-op",
    )


def _fresh_journal(journal_dir: Path):
    """Construct a `SessionJournal` for `journal_dir` with a fresh sanitizer."""

    from nora.core.session_journal import SessionJournal
    from nora.sanitizer import Sanitizer

    return SessionJournal(
        journal_dir=journal_dir,
        max_trace_steps=50,
        sanitizer=Sanitizer(),
        enabled=True,
    )


# ---------------------------------------------------------------------------
# R1 — first tool call creates the canonical file with all 7 keys
# ---------------------------------------------------------------------------


def test_first_tool_call_creates_canonical_file(journal_dir: Path) -> None:
    """R1-S1 — the first `record_step` writes `${session_id}.json` with all 7 fields.

    The file's `trace` has 1 entry (the step we just recorded); the other
    collection fields (`focus_device_id`, `devices_reviewed`) start empty.
    """
    journal = _fresh_journal(journal_dir)

    journal.record_step(
        tool="nora_health",
        input_args={},
        result_summary="ok",
        duration_ms=5,
        outcome="success",
        ts=datetime.now(timezone.utc),
    )

    # Exactly one JSON file exists in the journal dir.
    files = list(journal_dir.glob("*.json"))
    assert len(files) == 1, f"Expected one .json file; got {files!r}"
    with files[0].open() as f:
        state = json.load(f)

    required = {
        "session_id",
        "operator_alias",
        "started_at",
        "focus_device_id",
        "devices_reviewed",
        "trace",
        "last_updated",
    }
    assert required.issubset(state.keys()), (
        f"Missing keys in canonical file: {required - state.keys()}"
    )
    assert uuid.UUID(state["session_id"]).version == 4
    assert state["focus_device_id"] is None
    assert state["devices_reviewed"] == []
    # `trace` has 1 entry after the first tool call — not the empty default.
    assert len(state["trace"]) == 1
    assert state["trace"][0]["tool"] == "nora_health"
    # And the file's session_id matches the journal's session_id.
    assert state["session_id"] == journal.session_id


# ---------------------------------------------------------------------------
# R2 — successful tool call appends one Step
# ---------------------------------------------------------------------------


def test_successful_tool_call_appends_one_step(journal_dir: Path) -> None:
    """R2-S1 — a successful `record_step` appends exactly one step with step==1."""
    from datetime import datetime, timezone

    journal = _fresh_journal(journal_dir)

    journal.record_step(
        tool="nora_health",
        input_args={},
        result_summary="connectivity=ok",
        duration_ms=12,
        outcome="success",
        ts=datetime.now(timezone.utc),
    )

    state = journal.get_state()
    assert len(state.trace) == 1
    step = state.trace[0]
    assert step.step == 1
    assert step.outcome == "success"
    assert step.tool == "nora_health"


def test_llm_interpretation_defaults_to_none(journal_dir: Path) -> None:
    """R2-S3 — `llm_interpretation` defaults to None when not supplied."""
    from datetime import datetime, timezone

    journal = _fresh_journal(journal_dir)

    journal.record_step(
        tool="nora_health",
        input_args={},
        result_summary="ok",
        duration_ms=0,
        outcome="success",
        ts=datetime.now(timezone.utc),
    )

    state = journal.get_state()
    assert state.trace[0].llm_interpretation is None


# ---------------------------------------------------------------------------
# R3 — persistence fires before the wrapper returns
# ---------------------------------------------------------------------------


def test_persistence_fires_before_record_step_returns(
    journal_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R3-S1 — `atomic_write_json` runs BEFORE `record_step` returns."""
    from datetime import datetime, timezone

    from nora.core.session_paths import atomic_write_json

    journal = _fresh_journal(journal_dir)
    calls: list[bool] = []

    def spy(path: Path, payload: dict) -> None:
        calls.append(True)
        atomic_write_json(path, payload)

    monkeypatch.setattr("nora.core.session_journal.atomic_write_json", spy)

    journal.record_step(
        tool="nora_health",
        input_args={},
        result_summary="ok",
        duration_ms=1,
        outcome="success",
        ts=datetime.now(timezone.utc),
    )

    assert calls, "atomic_write_json was not called during record_step"


def test_mid_write_crash_leaves_parseable_file(
    journal_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R3-S2 — a crash mid-write leaves either old or new content (parseable JSON)."""
    from datetime import datetime, timezone

    journal = _fresh_journal(journal_dir)

    # Write a baseline step so the canonical file already exists with V1.
    journal.record_step(
        tool="first",
        input_args={},
        result_summary="V1",
        duration_ms=1,
        outcome="success",
        ts=datetime.now(timezone.utc),
    )

    # Now simulate a crash by raising inside atomic_write_json.
    def crashing_write(path: Path, payload: dict) -> None:
        raise OSError("simulated mid-write crash")

    monkeypatch.setattr("nora.core.session_journal.atomic_write_json", crashing_write)

    with pytest.raises(OSError):
        journal.record_step(
            tool="second",
            input_args={},
            result_summary="V2",
            duration_ms=1,
            outcome="success",
            ts=datetime.now(timezone.utc),
        )

    # The canonical file still parses as JSON.
    files = list(journal_dir.glob("*.json"))
    assert files, "Canonical file vanished after crash"
    with files[0].open() as f:
        state = json.load(f)
    # Either old content (V1) or already-written V2 is acceptable.
    assert "trace" in state and "session_id" in state


# ---------------------------------------------------------------------------
# R7-S2 — get_state on missing file creates empty state
# ---------------------------------------------------------------------------


def test_load_or_create_on_missing_file_creates_empty_state(journal_dir: Path) -> None:
    """R7-S2 — `get_state` on a fresh journal returns an empty state and creates the file."""
    journal = _fresh_journal(journal_dir)

    assert journal_dir.exists()
    assert not list(journal_dir.glob("*.json")), "Test setup error: file already exists"

    state = journal.get_state()
    assert state.trace == []
    assert state.devices_reviewed == []
    assert state.focus_device_id is None

    # And a canonical file was created.
    files = list(journal_dir.glob("*.json"))
    assert len(files) == 1
    with files[0].open() as f:
        on_disk = json.load(f)
    assert on_disk["trace"] == []


# ---------------------------------------------------------------------------
# R9 — session_id stable across many writes
# ---------------------------------------------------------------------------


def test_session_id_stable_across_many_writes(journal_dir: Path) -> None:
    """R9-S1 — `session_id` is identical across 5 consecutive `record_step` calls."""
    from datetime import datetime, timezone

    journal = _fresh_journal(journal_dir)
    initial_sid = journal.session_id

    for i in range(5):
        journal.record_step(
            tool=f"tool-{i}",
            input_args={"i": i},
            result_summary="ok",
            duration_ms=1,
            outcome="success",
            ts=datetime.now(timezone.utc),
        )

    state = journal.get_state()
    assert state.session_id == initial_sid
    assert all(s.tool == f"tool-{i}" for i, s in enumerate(state.trace))
