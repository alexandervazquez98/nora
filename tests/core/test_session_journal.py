"""SessionJournal core — happy-path recording contract.

This file accumulates tests as features land:
    task #4 — R1, R2, R3, R7-S2, R9 (load / save / append / get_state)
    task #5 — R6, R10 (redaction + sanitizer)
    task #6 — R5 (NDJSON rotation)
    task #7 — R12 (4-tuple preserved)
    task #11 — R11, R18 (corrupt + POSIX mode)
    task #10 — R15 (disable switch)
    remediation — R4-S2 (concurrent writers), R6-S2 (re-sanitize on read)
"""

from __future__ import annotations

import json
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
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


# ---------------------------------------------------------------------------
# R6 — free-text sanitization boundary (write + read).
# ---------------------------------------------------------------------------


def test_private_ipv4_in_llm_interpretation_is_masked_on_write(journal_dir: Path) -> None:
    """R6-S1 — a private IPv4 in `llm_interpretation` is masked before persistence.

    The alias appears on disk; the literal `10.0.0.5` MUST NOT survive.
    """
    from datetime import datetime, timezone

    journal = _fresh_journal(journal_dir)
    journal.record_step(
        tool="nora_health",
        input_args={},
        result_summary="ok",
        duration_ms=0,
        outcome="success",
        ts=datetime.now(timezone.utc),
        llm_interpretation="investigation at 10.0.0.5",
    )

    files = list(journal_dir.glob("*.json"))
    raw = files[0].read_text()
    assert "10.0.0.5" not in raw, f"Private IPv4 literal leaked to disk: {raw!r}"
    # The on-disk literal was replaced by an alias-shaped token.
    assert "RADIO_NODE_" in raw, f"Expected an alias on disk; got: {raw!r}"


def test_structured_fields_bypass_sanitizer(journal_dir: Path) -> None:
    """R6-S3 — `tool` / `step` / `duration_ms` MUST be byte-identical on read."""
    from datetime import datetime, timezone

    journal = _fresh_journal(journal_dir)
    journal.record_step(
        tool="nora_health",
        input_args={},
        result_summary="ok",
        duration_ms=12,
        outcome="success",
        ts=datetime.now(timezone.utc),
    )

    state = journal.get_state()
    step = state.trace[0]
    assert step.tool == "nora_health"
    assert step.step == 1
    assert step.duration_ms == 12


def test_result_summary_is_sanitized_on_write(journal_dir: Path) -> None:
    """R6 — `result_summary` runs through Sanitizer on write.

    A hostname literal in the summary MUST be replaced by an alias.
    """
    from datetime import datetime, timezone

    journal = _fresh_journal(journal_dir)
    journal.record_step(
        tool="nora_health",
        input_args={},
        result_summary="probe failed at router-core-01.example.com",
        duration_ms=0,
        outcome="success",
        ts=datetime.now(timezone.utc),
    )

    files = list(journal_dir.glob("*.json"))
    raw = files[0].read_text()
    assert "router-core-01.example.com" not in raw, f"Hostname literal leaked to disk: {raw!r}"
    assert "HOST_" in raw, f"Expected a HOST_ alias on disk; got: {raw!r}"


# ---------------------------------------------------------------------------
# R10 — name-keyed redaction on the persisted step's `input`.
# ---------------------------------------------------------------------------


def test_redacted_input_key_does_not_appear_on_disk(journal_dir: Path) -> None:
    """R10 — `kwargs["community"]` value MUST NOT survive the write.

    The literal `private` (the value) MUST NOT be in the canonical file.
    The marker `[REDACTED]` MUST appear in the on-disk JSON.
    """
    from datetime import datetime, timezone

    journal = _fresh_journal(journal_dir)
    journal.record_step(
        tool="snmp_get",
        input_args={"device_id": "ap-7400-01", "community": "private"},
        result_summary="ok",
        duration_ms=1,
        outcome="success",
        ts=datetime.now(timezone.utc),
    )

    files = list(journal_dir.glob("*.json"))
    raw = files[0].read_text()
    assert "private" not in raw, f"Redacted value leaked: {raw!r}"
    assert "[REDACTED]" in raw, f"Expected redaction marker on disk; got: {raw!r}"


# ---------------------------------------------------------------------------
# R11 — corrupt-file handling: typed error at read, auto-recovery on next write
# ---------------------------------------------------------------------------


def test_corrupt_file_raises_journal_corrupt_error(journal_dir: Path) -> None:
    """R11-S1 — invalid JSON bytes at the canonical path raise `JournalCorruptError`."""
    from nora.core.session_journal import JournalCorruptError, SessionJournal
    from nora.sanitizer import Sanitizer

    journal = SessionJournal(
        journal_dir=journal_dir,
        max_trace_steps=50,
        sanitizer=Sanitizer(),
        enabled=True,
    )

    # Pre-create the canonical file with bytes that are NOT valid JSON.
    sid = journal.session_id
    bad_path = journal_dir / f"{sid}.json"
    bad_path.write_text("{not valid json at all -- oops")

    with pytest.raises(JournalCorruptError) as excinfo:
        journal.get_state()

    # The path is included so the operator knows which file is bad.
    assert str(bad_path) in str(excinfo.value) or bad_path.name in str(excinfo.value), (
        f"Expected the corrupt path in the message; got: {excinfo.value!r}"
    )


def test_next_write_auto_recovers_by_archiving_corrupt(journal_dir: Path) -> None:
    """R11-S2 — the next write auto-recovers by archiving the corrupt file."""
    from datetime import datetime, timezone

    from nora.core.session_journal import JournalCorruptError, SessionJournal
    from nora.sanitizer import Sanitizer

    journal = SessionJournal(
        journal_dir=journal_dir,
        max_trace_steps=50,
        sanitizer=Sanitizer(),
        enabled=True,
    )
    sid = journal.session_id
    bad_path = journal_dir / f"{sid}.json"
    bad_path.write_text("{not json}")

    # First call raises; the file is still corrupt.
    with pytest.raises(JournalCorruptError):
        journal.get_state()

    # Next write auto-recovers.
    journal.record_step(
        tool="probe",
        input_args={},
        result_summary="ok",
        duration_ms=1,
        outcome="success",
        ts=datetime.now(timezone.utc),
    )

    # An archived .corrupt-*.json file exists.
    archived = list(journal_dir.glob(f"{sid}.corrupt-*.json"))
    assert len(archived) == 1, f"Expected one archived corrupt file; got {archived!r}"
    assert archived[0].read_text() == "{not json}", (
        "Archived file should preserve the original bad bytes"
    )

    # The canonical file is fresh and parses.
    fresh = list(journal_dir.glob(f"{sid}.json"))
    assert len(fresh) == 1
    state = journal.get_state()
    assert state.session_id == sid
    assert len(state.trace) == 1


# ---------------------------------------------------------------------------
# R18 — POSIX 0o600 mode on the canonical file
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="0o600 is POSIX-only")
def test_canonical_file_mode_0o600_on_posix(journal_dir: Path) -> None:
    """R18-S1 — the file mode after `_persist` is exactly 0o600 on POSIX."""
    from datetime import datetime, timezone

    journal = _fresh_journal(journal_dir)
    journal.record_step(
        tool="nora_health",
        input_args={},
        result_summary="ok",
        duration_ms=1,
        outcome="success",
        ts=datetime.now(timezone.utc),
    )

    files = list(journal_dir.glob("*.json"))
    assert len(files) == 1
    mode = files[0].stat().st_mode & 0o777
    assert mode == 0o600, f"Expected mode 0o600; got 0o{mode:o}"


# ---------------------------------------------------------------------------
# R6-S2 — re-sanitization on read masks anything that slipped past write
# ---------------------------------------------------------------------------


def test_resanitize_on_read_masks_bypass(journal_dir: Path) -> None:
    """R6-S2 — a free-text field containing a literal identifier on disk is masked on read.

    Simulates a sanitize bypass at write time: the on-disk file is hand-
    written with literal IPv4 and MAC addresses in two free-text fields.
    R6 defense-in-depth: every read MUST re-apply the Sanitizer to free-
    text fields, so the literals do NOT survive `get_state()`.

    Also confirms a structured field (`focus_device_id`) is NOT touched by
    re-sanitization (matches R6-S3 — structured fields bypass sanitizer).
    """
    import json as json_mod

    from nora.core.session_journal import SessionJournal
    from nora.sanitizer import Sanitizer

    journal = SessionJournal(
        journal_dir=journal_dir,
        max_trace_steps=50,
        sanitizer=Sanitizer(),
        enabled=True,
    )
    sid = journal.session_id
    canonical = journal_dir / f"{sid}.json"

    # Hand-write a session file whose free-text fields carry literal
    # IPv4 + MAC — simulating a sanitize bypass at write time.
    canonical.write_text(
        json_mod.dumps(
            {
                "session_id": sid,
                "operator_alias": "test-op",
                "started_at": "2026-01-01T00:00:00+00:00",
                "focus_device_id": "ap-7400-01",  # structured — must survive
                "devices_reviewed": ["ap-7400-01"],
                "last_updated": "2026-01-01T00:00:00+00:00",
                "trace": [
                    {
                        "ts": "2026-01-01T00:00:00+00:00",
                        "step": 1,
                        "tool": "nora_health",
                        "input": {},
                        "result_summary": "probe failed; mac=aa:bb:cc:dd:ee:ff",
                        "duration_ms": 12,
                        "outcome": "success",
                        "llm_interpretation": "investigation at 10.0.0.5",
                    }
                ],
            }
        )
    )

    # Read via get_state — R6-S2 requires the literals to be masked.
    state = journal.get_state()
    step = state.trace[0]

    # Free-text fields are re-sanitized: literals do NOT survive.
    assert "10.0.0.5" not in step.llm_interpretation, (
        f"Private IPv4 literal leaked through re-sanitize: {step.llm_interpretation!r}"
    )
    assert "aa:bb:cc:dd:ee:ff" not in step.result_summary, (
        f"MAC literal leaked through re-sanitize: {step.result_summary!r}"
    )
    # Synthetic aliases appear in the returned trace.
    assert "RADIO_NODE_" in step.llm_interpretation
    assert "SWITCH_ACC_" in step.result_summary

    # Structured fields bypass re-sanitization (R6-S3).
    assert state.focus_device_id == "ap-7400-01"


# ---------------------------------------------------------------------------
# R4-S2 — concurrent writers both end with valid JSON
# ---------------------------------------------------------------------------


def test_concurrent_writers_both_end_with_valid_json(journal_dir: Path) -> None:
    """R4-S2 — N threads racing `record_step` leave a parseable JSON file.

    The design contract is deterministic last-write-wins: each writer
    builds an in-memory copy of the state, appends its step, and calls
    `atomic_write_json` (temp + `os.replace`). The final on-disk file
    MUST be parseable as JSON; the trace length MUST be one of
    ``{1, 2, ..., N}`` (some writes may be lost to last-write-wins; the
    file MUST never be a torn mix).

    The test uses a `threading.Barrier` so all writers enter
    `record_step` simultaneously, maximising the contention window at
    the `_load_or_create → state.trace.append → _persist` sequence.
    """
    from datetime import datetime, timezone

    from nora.core.session_journal import SessionJournal
    from nora.sanitizer import Sanitizer

    journal = SessionJournal(
        journal_dir=journal_dir,
        max_trace_steps=50,
        sanitizer=Sanitizer(),
        enabled=True,
    )
    # All writers race on the SAME journal + SAME canonical file. We
    # share a session by having every writer use the same instance; the
    # last `_persist` write is the on-disk survivor.
    n_writers = 4
    barrier = threading.Barrier(n_writers)

    def writer(i: int) -> None:
        # All threads block here until N writers have arrived.
        barrier.wait()
        # Use the journal's own session_id so they all append to the
        # same canonical file. Each writer appends ONE distinct step.
        journal.record_step(
            tool=f"writer-{i}",
            input_args={"i": i},
            result_summary=f"step from writer-{i}",
            duration_ms=1,
            outcome="success",
            ts=datetime.now(timezone.utc),
        )

    with ThreadPoolExecutor(max_workers=n_writers) as pool:
        futures = [pool.submit(writer, i) for i in range(n_writers)]
        for f in futures:
            f.result()

    # Exactly one canonical file must exist; the .tmp orphan (if any)
    # is not a valid JSON file and is ignored.
    files = [p for p in journal_dir.glob("*.json") if not p.name.endswith(".tmp")]
    assert len(files) == 1, f"Expected one canonical file; got {files!r}"

    # The canonical file MUST be parseable as JSON. This is the
    # load-bearing assertion: any torn mix fails json.load.
    with files[0].open() as f:
        data = json.load(f)

    assert "trace" in data and isinstance(data["trace"], list)

    # Trace length is bounded by [1, N_writers]. The design accepts
    # last-write-wins (some writes can be lost). The contract is "never
    # a torn mix" — which is equivalent to "valid JSON above" since
    # torn bytes would not parse.
    trace_len = len(data["trace"])
    assert 1 <= trace_len <= n_writers, (
        f"Trace length {trace_len} outside the bounded last-write-wins contract [1, {n_writers}]"
    )

    # Every step on disk is a valid SessionStep dict (tool field present).
    for s in data["trace"]:
        assert s["tool"].startswith("writer-"), f"Step tool name is not a writer marker: {s!r}"
        assert s["step"] >= 1
