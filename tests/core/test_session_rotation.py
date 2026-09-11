"""NDJSON rotation — R5 boundary tests.

Threshold: when `len(trace) > max_steps`, the oldest step is displaced
to NDJSON. The canonical trace stays bounded.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def _fresh_journal(journal_dir: Path):
    from nora.core.session_journal import SessionJournal
    from nora.sanitizer import Sanitizer

    return SessionJournal(
        journal_dir=journal_dir,
        max_trace_steps=3,
        sanitizer=Sanitizer(),
        enabled=True,
    )


def _record(journal, *, tool: str, summary: str = "ok"):
    journal.record_step(
        tool=tool,
        input_args={},
        result_summary=summary,
        duration_ms=1,
        outcome="success",
        ts=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# R5-S1 — appending past the threshold rotates the oldest step
# ---------------------------------------------------------------------------


def test_rotate_displaces_oldest_step_to_ndjson(journal_dir: Path) -> None:
    """When `len(trace) == max_steps` and a new step arrives, step 1 goes to NDJSON."""
    journal = _fresh_journal(journal_dir)

    # Record 4 steps; with max_steps=3, step 1 must be displaced.
    for i in range(1, 5):
        _record(journal, tool=f"tool-{i}")

    # NDJSON exists with exactly one line whose `step` integer is 1.
    ndjson_files = list(journal_dir.glob("*.log.ndjson"))
    assert len(ndjson_files) == 1, f"Expected one NDJSON file; got {ndjson_files!r}"
    raw = ndjson_files[0].read_text().splitlines()
    assert len(raw) == 1, f"Expected one NDJSON line; got {raw!r}"
    parsed = json.loads(raw[0])
    assert parsed["step"] == 1

    # Canonical trace has length 3 (steps 2, 3, 4).
    state = journal.get_state()
    assert len(state.trace) == 3
    assert [s.step for s in state.trace] == [2, 3, 4]


# ---------------------------------------------------------------------------
# R5-S2 — NDJSON is append-only across multiple rotations
# ---------------------------------------------------------------------------


def test_ndjson_is_append_only_across_rotations(journal_dir: Path) -> None:
    """Two consecutive rotations append displaced steps in displacement order."""
    journal = _fresh_journal(journal_dir)

    # Record 5 steps; with max_steps=3, rotations displace steps 1 then 2.
    for i in range(1, 6):
        _record(journal, tool=f"tool-{i}")

    ndjson_files = list(journal_dir.glob("*.log.ndjson"))
    assert len(ndjson_files) == 1
    lines = ndjson_files[0].read_text().splitlines()
    assert len(lines) == 2, f"Expected 2 NDJSON lines; got {lines!r}"
    parsed = [json.loads(line) for line in lines]
    # Displacement order: step 1 first, then step 2.
    assert [p["step"] for p in parsed] == [1, 2]

    # Canonical trace has steps 3, 4, 5.
    state = journal.get_state()
    assert [s.step for s in state.trace] == [3, 4, 5]


# ---------------------------------------------------------------------------
# Below the threshold — no rotation happens.
# ---------------------------------------------------------------------------


def test_no_rotation_below_threshold(journal_dir: Path) -> None:
    """When `len(trace) <= max_steps`, no NDJSON file is created."""
    journal = _fresh_journal(journal_dir)

    for i in range(1, 4):
        _record(journal, tool=f"tool-{i}")

    ndjson_files = list(journal_dir.glob("*.log.ndjson"))
    assert ndjson_files == [], f"No NDJSON should exist below threshold; got {ndjson_files!r}"

    state = journal.get_state()
    assert len(state.trace) == 3


# ---------------------------------------------------------------------------
# Rotation hook fired through SessionJournal.record_step — drives the boundary.
# ---------------------------------------------------------------------------


def test_record_step_rotates_at_threshold(journal_dir: Path) -> None:
    """`SessionJournal.record_step` MUST invoke rotation when the threshold is crossed."""
    journal = _fresh_journal(journal_dir)

    # Init: trace len = 0, max = 3. Record 4; expect rotation on the 4th.
    for i in range(1, 5):
        _record(journal, tool=f"tool-{i}")

    state = journal.get_state()
    assert len(state.trace) == 3, (
        f"After 4 records with max=3, trace should be 3; got {len(state.trace)}"
    )
    # Step 1 displaced to NDJSON.
    ndjson = list(journal_dir.glob("*.log.ndjson"))
    assert len(ndjson) == 1
    lines = ndjson[0].read_text().splitlines()
    assert json.loads(lines[0])["step"] == 1
