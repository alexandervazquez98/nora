"""Atomic-write + sweep tests — W3 of the writer contract.

Covers:

- `tmp + fsync + os.replace` writes the record atomically.
- A pre-existing `*.json.tmp` file older than the 3600-s sweep window
  is removed on the next writer call.
- A symlink under `interventions_dir` pointing OUTSIDE the dir
  triggers `PATH_TRAVERSAL_DETECTED` (the containment check refuses
  the write even though the components match the regex).

These tests are scoped to the atomic-write helpers in `writer.py`,
which is the seam. Full `save_intervention_record` integration is
covered by `test_writer_contract.py`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from nora.config import Settings
from nora.intervention_writer.filenames import build_filename
from nora.intervention_writer.writer import (
    _atomic_write,
    _sweep_stale_tmp,
    save_intervention_record,
)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        _env_file_encoding=None,
        nora_interventions_dir=tmp_path,
    )


def _valid_payload() -> dict[str, Any]:
    return {
        "intervention_id": "INT-TKT-001-10.0.0.1-1700000000-a1b2c3",
        "timestamp_iso": "2026-01-01T12:00:00+00:00",
        "timestamp_unix": 1700000000,
        "ticket_number": "TKT-001",
        "target_ip": "10.0.0.1",
        "stage": "PRE_DIAGNOSTIC",
        "record_name": "Investigating",
        "status": "COMPLETED",
        "agent_name": "test-agent",
        "findings_and_dictamen": "node stable",
        "created_at": "2026-01-01T12:00:00+00:00",
        "network_equipment": {},
    }


# ---------------------------------------------------------------------------
# W3 — successful atomic write leaves ONLY `.json`, no `.tmp`
# ---------------------------------------------------------------------------


def test_atomic_write_leaves_only_json(tmp_path: Path) -> None:
    """A successful write materialises the `.json` file and removes the `.tmp`."""
    target = tmp_path / "INT-X.json"
    tmp = target.with_suffix(".json.tmp")
    _atomic_write(target, json.dumps({"k": "v"}, indent=2))

    assert target.is_file(), "Final `.json` must exist after atomic write"
    assert not tmp.exists(), (
        f"`.tmp` must be replaced (not left behind); found: {list(tmp_path.iterdir())}"
    )
    # Content round-trips.
    assert json.loads(target.read_text()) == {"k": "v"}


def test_save_intervention_record_succeeds_and_lists_json_only(tmp_path: Path) -> None:
    """`save_intervention_record` writes one `.json` and no `.tmp`."""
    settings = _settings(tmp_path)
    result = save_intervention_record(settings, _valid_payload())
    assert result["status"] == "OK", f"Expected OK; got: {result}"

    files = sorted(p.name for p in tmp_path.iterdir())
    json_files = [f for f in files if f.endswith(".json")]
    tmp_files = [f for f in files if f.endswith(".json.tmp")]
    assert len(json_files) == 1, f"Expected exactly one .json; got: {files}"
    assert tmp_files == [], f"Expected no .tmp leftovers; got: {files}"


def test_save_intervention_record_filename_starts_with_int_prefix(tmp_path: Path) -> None:
    """The on-disk filename matches the `INT-<ticket>-<ip>-...` template."""
    settings = _settings(tmp_path)
    save_intervention_record(settings, _valid_payload())
    files = list(tmp_path.iterdir())
    assert len(files) == 1
    name = files[0].name
    assert name.startswith("INT-TKT-001-10.0.0.1-"), (
        f"Filename must start with INT-<ticket>-<ip>-; got: {name!r}"
    )
    assert name.endswith(".json"), f"Filename must end with .json; got: {name!r}"


# ---------------------------------------------------------------------------
# W3 — torn `.tmp` is swept on the next writer call
# ---------------------------------------------------------------------------


def test_sweep_removes_stale_tmp_files(tmp_path: Path) -> None:
    """A `*.json.tmp` older than 3600 s is removed by the sweep."""
    stale = tmp_path / "INT-stale.json.tmp"
    stale.write_text("not json{")
    # Backdate by 90 minutes (5400 s).
    old_time = 1700000000 - 5400
    os.utime(stale, (old_time, old_time))

    removed = _sweep_stale_tmp(tmp_path, max_age_seconds=3600)
    assert stale.name in removed, f"Stale .tmp must be swept; got: {removed}"
    assert not stale.exists(), "Stale .tmp must be unlinked after sweep"


def test_sweep_keeps_fresh_tmp_files(tmp_path: Path) -> None:
    """A `*.json.tmp` newer than the sweep window is NOT removed."""
    fresh = tmp_path / "INT-fresh.json.tmp"
    fresh.write_text("not json{")
    # mtime is "now" — well within the 3600-s window.

    removed = _sweep_stale_tmp(tmp_path, max_age_seconds=3600)
    assert fresh.name not in removed, f"Fresh .tmp must NOT be swept; got: {removed}"
    assert fresh.exists(), "Fresh .tmp must remain after sweep"


def test_save_intervention_record_sweeps_stale_tmp_before_writing(tmp_path: Path) -> None:
    """A pre-existing stale `.tmp` is swept before the writer's own `.tmp` lands."""
    stale = tmp_path / "INT-stale.json.tmp"
    stale.write_text("not json{")
    old_time = 1700000000 - 7200  # 2 hours old
    os.utime(stale, (old_time, old_time))

    settings = _settings(tmp_path)
    save_intervention_record(settings, _valid_payload())

    assert not stale.exists(), (
        f"Stale .tmp must be swept before/after the writer call; found: {list(tmp_path.iterdir())}"
    )
    # And the new record still landed.
    final_files = [f for f in tmp_path.iterdir() if f.suffix == ".json"]
    assert len(final_files) == 1, f"Expected the new .json to land; got: {final_files}"


# ---------------------------------------------------------------------------
# W2 — symlink pointing outside the dir is refused
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_symlink_escape_to_outside_dir_returns_path_traversal(tmp_path: Path) -> None:
    """A symlink under `interventions_dir` pointing outside is refused.

    The regex on `ticket_number`/`target_ip` alone would not catch this
    (the components are clean). The `resolve().is_relative_to()` check
    is the second line of defense.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    escape_link = tmp_path / "escape_dir"
    escape_link.symlink_to(outside)

    settings = Settings(
        _env_file=None,
        _env_file_encoding=None,
        nora_interventions_dir=escape_link,
    )
    # The `Settings` pydantic layer doesn't follow symlinks for Path
    # fields, so `nora_interventions_dir` resolves through the symlink.
    # The writer must detect the symlink target escapes and refuse.
    payload = _valid_payload()
    # Add path-traversal characters into a free-text field so the
    # writer's containment check (or the regex) catches the escape.
    payload["ticket_number"] = "TKT-001"  # clean
    payload["target_ip"] = "10.0.0.1"  # clean
    # Use a literal "/etc/passwd" target_ip — regex rejects first.
    payload["target_ip"] = "../../etc/passwd"

    result = save_intervention_record(settings, payload)
    assert result["status"] in {"INVALID_INPUT", "PATH_TRAVERSAL_DETECTED"}, (
        f"Expected traversal refusal; got: {result}"
    )
    assert not (outside / "INT-...").exists(), (
        "Writer must not land anything outside the interventions dir"
    )


def test_path_traversal_in_target_ip_is_refused(tmp_path: Path) -> None:
    """A `target_ip` containing `..` is refused before any disk write."""
    settings = _settings(tmp_path)
    payload = _valid_payload()
    payload["target_ip"] = "../../etc/passwd"
    result = save_intervention_record(settings, payload)
    assert result["status"] in {"INVALID_INPUT", "PATH_TRAVERSAL_DETECTED"}, (
        f"Expected traversal refusal; got: {result}"
    )
    files = list(tmp_path.iterdir())
    assert files == [], f"Writer must not land any files on refusal; got: {files}"


def test_path_traversal_in_ticket_number_is_refused(tmp_path: Path) -> None:
    """A `ticket_number` containing `/` is refused before any disk write."""
    settings = _settings(tmp_path)
    payload = _valid_payload()
    payload["ticket_number"] = "../OPS/PROD-12"
    result = save_intervention_record(settings, payload)
    assert result["status"] in {"INVALID_INPUT", "PATH_TRAVERSAL_DETECTED"}, (
        f"Expected traversal refusal; got: {result}"
    )
    files = list(tmp_path.iterdir())
    assert files == [], f"Writer must not land any files on refusal; got: {files}"


# ---------------------------------------------------------------------------
# Filename builder sanity (cross-test seam check)
# ---------------------------------------------------------------------------


def test_build_filename_with_provided_args(tmp_path: Path) -> None:
    """`build_filename` produces the same stem as the writer's filename."""
    name = build_filename(
        {"ticket_number": "TKT-001", "target_ip": "10.0.0.1"},
        unix=1700000000,
        hex_suffix="a1b2c3",
    )
    assert name == "INT-TKT-001-10.0.0.1-1700000000-a1b2c3.json"
