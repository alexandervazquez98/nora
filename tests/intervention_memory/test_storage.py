"""Storage layer tests — covers R3 (tolerant read + missing-dir safety).

Mirrors `src/nora/core/session_journal.py:_load_or_create_or_recover`:
per-file `try/except (json.JSONDecodeError, ValidationError)` →
`logger.warning(filename); continue`. The storage layer MUST NOT create
the interventions directory if absent (NORA does not own the dir;
openchat's writer does).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest


def _write_record(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload))


def _make_valid_record(
    intervention_id: str = "INT-X",
    target_ip: str = "192.0.2.50",
    timestamp_unix: int = 1700003000,
    stage: str = "PRE_DIAGNOSTIC",
) -> dict[str, Any]:
    return {
        "intervention_id": intervention_id,
        "timestamp_iso": "2026-01-01T12:00:00+00:00",
        "timestamp_unix": timestamp_unix,
        "ticket_number": "TKT-0001",
        "target_ip": target_ip,
        "stage": stage,
        "record_name": "synthetic record",
        "status": "COMPLETED",
        "agent_name": "test-agent",
        "findings_and_dictamen": "no findings",
        "created_at": "2026-01-01T12:00:00+00:00",
        "network_equipment": {},
    }


# ---------------------------------------------------------------------------
# R3-S1 — corrupt JSON file is skipped
# ---------------------------------------------------------------------------


def test_corrupt_json_file_is_skipped(tmp_path: Path) -> None:
    """3 valid + 1 `}.invalid.json` with `{not json` → 3 records, no raise."""
    from nora.intervention_memory.storage import read_records

    settings = type("S", (), {"nora_interventions_dir": tmp_path})()

    # 3 valid records.
    for idx in range(3):
        _write_record(tmp_path / f"rec-{idx}.json", _make_valid_record(intervention_id=f"INT-{idx}"))
    # 1 corrupt record — filename intentionally starts with `}`.
    (tmp_path / "}.invalid.json").write_text("{not json")

    records = read_records(settings)

    assert len(records) == 3, (
        f"Expected 3 valid records (corrupt skipped); got: {len(records)}"
    )


def test_corrupt_json_emits_warning_with_filename(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """R3-S1 — WARNING log line names the corrupt file."""
    from nora.intervention_memory.storage import read_records

    settings = type("S", (), {"nora_interventions_dir": tmp_path})()
    _write_record(tmp_path / "ok.json", _make_valid_record(intervention_id="INT-OK"))
    corrupt_name = "}.invalid.json"
    (tmp_path / corrupt_name).write_text("{not json")

    with caplog.at_level(logging.WARNING, logger="nora.intervention_memory.storage"):
        read_records(settings)

    warning_lines = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(corrupt_name in line for line in warning_lines), (
        f"Expected a WARNING log line naming '{corrupt_name}'; got: {warning_lines!r}"
    )


# ---------------------------------------------------------------------------
# R3-S2 — pydantic ValidationError is skipped
# ---------------------------------------------------------------------------


def test_validation_error_is_skipped(tmp_path: Path) -> None:
    """1 valid + 1 file with bogus `stage` literal → 1 record, no raise."""
    from nora.intervention_memory.storage import read_records

    settings = type("S", (), {"nora_interventions_dir": tmp_path})()
    _write_record(tmp_path / "ok.json", _make_valid_record(intervention_id="INT-OK"))
    # validation_failure.json has `stage: BOGUS_STAGE` → ValidationError.
    fixture = Path(__file__).resolve().parent.parent.parent / "tests" / "fixtures" / "intervention_memory" / "validation_failure.json"
    (tmp_path / "validation_failure.json").write_text(fixture.read_text())

    records = read_records(settings)

    assert len(records) == 1
    assert records[0].intervention_id == "INT-OK"


def test_validation_error_emits_warning_with_filename(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """R3-S2 — WARNING log line names the validation-failing file."""
    from nora.intervention_memory.storage import read_records

    settings = type("S", (), {"nora_interventions_dir": tmp_path})()
    _write_record(tmp_path / "ok.json", _make_valid_record(intervention_id="INT-OK"))
    fixture = Path(__file__).resolve().parent.parent.parent / "tests" / "fixtures" / "intervention_memory" / "validation_failure.json"
    (tmp_path / "bad.json").write_text(fixture.read_text())

    with caplog.at_level(logging.WARNING, logger="nora.intervention_memory.storage"):
        read_records(settings)

    warning_lines = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("bad.json" in line for line in warning_lines), (
        f"Expected a WARNING log line naming 'bad.json'; got: {warning_lines!r}"
    )


# ---------------------------------------------------------------------------
# R3-S3 — missing directory returns empty results, does NOT create it
# ---------------------------------------------------------------------------


def test_missing_directory_returns_empty_list(tmp_path: Path) -> None:
    """A non-existent `nora_interventions_dir` returns `[]` and does NOT create the dir."""
    from nora.intervention_memory.storage import read_records

    absent_dir = tmp_path / "this-path-does-not-exist"
    settings = type("S", (), {"nora_interventions_dir": absent_dir})()

    records = read_records(settings)

    assert records == []
    assert not absent_dir.exists(), (
        f"Storage layer must NOT create missing dir; but {absent_dir} now exists."
    )


def test_empty_directory_returns_empty_list(tmp_path: Path) -> None:
    """An empty directory returns `[]`."""
    from nora.intervention_memory.storage import read_records

    settings = type("S", (), {"nora_interventions_dir": tmp_path})()

    records = read_records(settings)

    assert records == []


# ---------------------------------------------------------------------------
# Triangulate: glob picks up every *.json in the dir
# ---------------------------------------------------------------------------


def test_glob_picks_up_every_json_file(tmp_path: Path) -> None:
    """NORA does NOT inspect filenames — glob `*.json` matches every JSON file."""
    from nora.intervention_memory.storage import read_records

    settings = type("S", (), {"nora_interventions_dir": tmp_path})()
    for idx in range(5):
        _write_record(
            tmp_path / f"TKT-7400_192.0.2.{idx}_PRE_DIAGNOSTIC_{1700000000 + idx}.json",
            _make_valid_record(intervention_id=f"INT-{idx}"),
        )

    records = read_records(settings)

    assert len(records) == 5


def test_non_json_files_are_ignored(tmp_path: Path) -> None:
    """`.txt`, `.bak`, etc. are ignored by the `*.json` glob."""
    from nora.intervention_memory.storage import read_records

    settings = type("S", (), {"nora_interventions_dir": tmp_path})()
    _write_record(tmp_path / "rec.json", _make_valid_record(intervention_id="INT-1"))
    (tmp_path / "readme.txt").write_text("ignore me")
    (tmp_path / "data.bak").write_text("ignore me too")

    records = read_records(settings)

    assert len(records) == 1
    assert records[0].intervention_id == "INT-1"


# ---------------------------------------------------------------------------
# Triangulate: records are returned as InterventionMemoryRecord instances
# ---------------------------------------------------------------------------


def test_records_are_pydantic_instances(tmp_path: Path) -> None:
    """Returned objects are `InterventionMemoryRecord` (not raw dicts)."""
    from nora.intervention_memory.models import InterventionMemoryRecord
    from nora.intervention_memory.storage import read_records

    settings = type("S", (), {"nora_interventions_dir": tmp_path})()
    _write_record(tmp_path / "rec.json", _make_valid_record(intervention_id="INT-X"))

    records = read_records(settings)

    assert all(isinstance(r, InterventionMemoryRecord) for r in records)


def test_records_have_no_extra_keys(tmp_path: Path) -> None:
    """Extra keys on disk are silently dropped (extra='ignore' contract)."""
    from nora.intervention_memory.storage import read_records

    settings = type("S", (), {"nora_interventions_dir": tmp_path})()
    payload = _make_valid_record(intervention_id="INT-X")
    payload["future_unknown_field"] = "ignore me"
    payload["network_equipment"]["another_unknown"] = 42
    _write_record(tmp_path / "rec.json", payload)

    records = read_records(settings)

    dumped = records[0].model_dump(mode="json")
    assert "future_unknown_field" not in dumped
    assert "another_unknown" not in dumped["network_equipment"]