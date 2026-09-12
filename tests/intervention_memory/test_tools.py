"""Tools layer tests — covers R4 (search), R5 (lifecycle), R6 (correlate), R7 (cap).

The library functions in `nora.intervention_memory.tools` are the
source-of-truth for both the `@mcp.tool` wrappers in `src/nora/server.py`
and the webui.db mirror shim in `shim_webui.py`. The tests assert the
filtered library behaviour AND the shim delegation contract (R10).

Each task gets its own test group so a regression in one tool cannot
mask a regression in another.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest


def _write_record(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload))


def _make_record(
    *,
    intervention_id: str,
    target_ip: str,
    timestamp_unix: int,
    stage: str = "PRE_DIAGNOSTIC",
    ticket_number: str = "TKT-0001",
    record_name: str = "synthetic record",
    findings_and_dictamen: str = "no findings",
    system_name: str | None = "TWR-ISABEL-A",
    carrier_frequency_mhz: float | None = None,
) -> dict[str, Any]:
    ne: dict[str, Any] = {}
    if system_name is not None:
        ne["system_name"] = system_name
    if carrier_frequency_mhz is not None:
        ne["carrier_frequency_mhz"] = carrier_frequency_mhz
    return {
        "intervention_id": intervention_id,
        "timestamp_iso": "2026-01-01T12:00:00+00:00",
        "timestamp_unix": timestamp_unix,
        "ticket_number": ticket_number,
        "target_ip": target_ip,
        "stage": stage,
        "record_name": record_name,
        "status": "COMPLETED",
        "agent_name": "test-agent",
        "findings_and_dictamen": findings_and_dictamen,
        "created_at": "2026-01-01T12:00:00+00:00",
        "network_equipment": ne,
    }


@pytest.fixture
def tmp_interventions_dir(tmp_path: Path) -> Path:
    """An empty interventions directory under tmp_path."""
    return tmp_path


def _settings_for(tmp_path: Path, *, keyword_cap: int = 1000, correlate_limit: int = 50) -> Any:
    """Build a hermetic Settings-like object for tool calls.

    A real Settings requires `_env_file=None, _env_file_encoding=None` —
    but for these tests we just need an object with the right fields.
    """
    from nora.config import Settings

    return Settings(
        _env_file=None,
        _env_file_encoding=None,
        nora_interventions_dir=tmp_path,
        nora_interventions_keyword_search_max_records=keyword_cap,
        nora_interventions_correlate_scan_limit=correlate_limit,
    )


# ---------------------------------------------------------------------------
# R4-S1 — target_ip exact match
# ---------------------------------------------------------------------------


def test_search_target_ip_exact_match(tmp_path: Path) -> None:
    """`search(target_ip="10.0.0.5")` returns ONLY records whose target_ip equals exactly."""
    from nora.intervention_memory.tools import search_intervention_history
    from nora.sanitizer import Sanitizer

    settings = _settings_for(tmp_path)
    _write_record(tmp_path / "r1.json", _make_record(intervention_id="INT-1", target_ip="10.0.0.4", timestamp_unix=100))
    _write_record(tmp_path / "r2.json", _make_record(intervention_id="INT-2", target_ip="10.0.0.5", timestamp_unix=200))
    _write_record(tmp_path / "r3.json", _make_record(intervention_id="INT-3", target_ip="10.0.0.5", timestamp_unix=300))
    sanitizer = Sanitizer()

    results = search_intervention_history(settings, target_ip="10.0.0.5", sanitizer=sanitizer)

    assert len(results) == 2
    assert all(r["target_ip"] == "10.0.0.5" for r in results)
    # Sorted DESC by timestamp_unix.
    assert [r["timestamp_unix"] for r in results] == [300, 200]


def test_search_target_ip_no_match_returns_empty(tmp_path: Path) -> None:
    """`target_ip` that matches nothing returns `[]`."""
    from nora.intervention_memory.tools import search_intervention_history
    from nora.sanitizer import Sanitizer

    settings = _settings_for(tmp_path)
    _write_record(tmp_path / "r1.json", _make_record(intervention_id="INT-1", target_ip="10.0.0.4", timestamp_unix=100))
    sanitizer = Sanitizer()

    results = search_intervention_history(settings, target_ip="10.0.0.99", sanitizer=sanitizer)

    assert results == []


# ---------------------------------------------------------------------------
# R4-S2 — keyword substring match (uses json.dumps(record).lower())
# ---------------------------------------------------------------------------


def test_search_keyword_substring_match_uses_json_dumps_lowering(tmp_path: Path) -> None:
    """`keyword="interference"` matches records whose serialised form contains the substring (lowercased)."""
    from nora.intervention_memory.tools import search_intervention_history
    from nora.sanitizer import Sanitizer

    settings = _settings_for(tmp_path)
    _write_record(
        tmp_path / "r1.json",
        _make_record(
            intervention_id="INT-1",
            target_ip="10.0.0.4",
            timestamp_unix=100,
            findings_and_dictamen="Interference observed on the channel",
        ),
    )
    _write_record(
        tmp_path / "r2.json",
        _make_record(
            intervention_id="INT-2",
            target_ip="10.0.0.5",
            timestamp_unix=200,
            findings_and_dictamen="No anomalies detected",
        ),
    )
    sanitizer = Sanitizer()

    results = search_intervention_history(settings, keyword="interference", sanitizer=sanitizer)

    assert len(results) == 1
    assert results[0]["intervention_id"] == "INT-1"


def test_search_keyword_is_case_insensitive(tmp_path: Path) -> None:
    """`keyword="INTERFERENCE"` matches `interference observed` (lowered on both sides)."""
    from nora.intervention_memory.tools import search_intervention_history
    from nora.sanitizer import Sanitizer

    settings = _settings_for(tmp_path)
    _write_record(
        tmp_path / "r1.json",
        _make_record(
            intervention_id="INT-1",
            target_ip="10.0.0.4",
            timestamp_unix=100,
            findings_and_dictamen="INTERFERENCE observed",
        ),
    )
    sanitizer = Sanitizer()

    results = search_intervention_history(settings, keyword="interference", sanitizer=sanitizer)

    assert len(results) == 1


# ---------------------------------------------------------------------------
# R4-S3 — results sorted by timestamp_unix DESC
# ---------------------------------------------------------------------------


def test_results_sorted_by_timestamp_unix_descending(tmp_path: Path) -> None:
    """Records returned in timestamp_unix DESC regardless of insertion order."""
    from nora.intervention_memory.tools import search_intervention_history
    from nora.sanitizer import Sanitizer

    settings = _settings_for(tmp_path)
    # Inserted in non-sorted order.
    _write_record(tmp_path / "a.json", _make_record(intervention_id="INT-A", target_ip="10.0.0.1", timestamp_unix=100))
    _write_record(tmp_path / "c.json", _make_record(intervention_id="INT-C", target_ip="10.0.0.3", timestamp_unix=300))
    _write_record(tmp_path / "b.json", _make_record(intervention_id="INT-B", target_ip="10.0.0.2", timestamp_unix=200))
    sanitizer = Sanitizer()

    results = search_intervention_history(settings, sanitizer=sanitizer)

    assert [r["intervention_id"] for r in results] == ["INT-C", "INT-B", "INT-A"]


# ---------------------------------------------------------------------------
# R4-S4 — limit clamps result set
# ---------------------------------------------------------------------------


def test_limit_clamps_result_set(tmp_path: Path) -> None:
    """`limit=2` returns exactly 2 records (the most recent)."""
    from nora.intervention_memory.tools import search_intervention_history
    from nora.sanitizer import Sanitizer

    settings = _settings_for(tmp_path)
    for idx in range(5):
        _write_record(
            tmp_path / f"r{idx}.json",
            _make_record(intervention_id=f"INT-{idx}", target_ip="10.0.0.1", timestamp_unix=idx),
        )
    sanitizer = Sanitizer()

    results = search_intervention_history(settings, limit=2, sanitizer=sanitizer)

    assert len(results) == 2
    assert [r["timestamp_unix"] for r in results] == [4, 3]


# ---------------------------------------------------------------------------
# R4-S5 — free-text fields sanitized in output
# ---------------------------------------------------------------------------


def test_free_text_fields_in_search_output_are_sanitized(tmp_path: Path) -> None:
    """`record_name` containing an IP MUST be masked in the search output."""
    from nora.intervention_memory.tools import search_intervention_history
    from nora.sanitizer import Sanitizer

    settings = _settings_for(tmp_path)
    _write_record(
        tmp_path / "r1.json",
        _make_record(
            intervention_id="INT-1",
            target_ip="10.0.0.4",
            timestamp_unix=100,
            record_name="Investigating 10.0.0.5 today",
        ),
    )
    sanitizer = Sanitizer()

    results = search_intervention_history(settings, sanitizer=sanitizer)

    assert "10.0.0.5" not in results[0]["record_name"]
    assert "RADIO_NODE_" in results[0]["record_name"]


# ---------------------------------------------------------------------------
# R7-S1 / R7-S2 — keyword search I/O cap
# ---------------------------------------------------------------------------


def test_keyword_search_cap_enforced_and_warning_logged(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When `keyword` is provided AND record count exceeds the cap, read ≤ cap files + WARNING.

    Counts the number of file LOAD attempts (not glob yields). The cap
    fires at the read boundary, so `Path.glob` may yield more files than
    are loaded.
    """
    from nora.intervention_memory import storage as storage_mod
    from nora.intervention_memory.tools import search_intervention_history
    from nora.sanitizer import Sanitizer

    settings = _settings_for(tmp_path, keyword_cap=10)

    # Write 50 valid records.
    for idx in range(50):
        _write_record(
            tmp_path / f"r{idx:03d}.json",
            _make_record(intervention_id=f"INT-{idx}", target_ip="10.0.0.1", timestamp_unix=idx),
        )

    original_load_one = storage_mod._load_one
    load_count = 0

    def counting_load_one(path):
        nonlocal load_count
        load_count += 1
        return original_load_one(path)

    storage_mod._load_one = counting_load_one
    try:
        with caplog.at_level(logging.WARNING, logger="nora.intervention_memory.tools"):
            results = search_intervention_history(
                settings, keyword="anything", sanitizer=Sanitizer()
            )
    finally:
        storage_mod._load_one = original_load_one

    # The cap kicks in — at most `cap` files were loaded.
    assert load_count <= 10, f"Expected ≤ 10 loads under cap=10; got {load_count}"
    # A WARNING log line mentions the cap.
    warning_lines = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("cap" in line.lower() for line in warning_lines), (
        f"Expected cap-related WARNING; got: {warning_lines!r}"
    )


def test_keyword_search_under_cap_no_warning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Under the cap, NO cap-related WARNING is emitted."""
    from nora.intervention_memory.tools import search_intervention_history
    from nora.sanitizer import Sanitizer

    settings = _settings_for(tmp_path, keyword_cap=1000)

    for idx in range(5):
        _write_record(
            tmp_path / f"r{idx}.json",
            _make_record(intervention_id=f"INT-{idx}", target_ip="10.0.0.1", timestamp_unix=idx),
        )

    with caplog.at_level(logging.WARNING, logger="nora.intervention_memory.tools"):
        results = search_intervention_history(settings, keyword="synthetic", sanitizer=Sanitizer())

    assert len(results) == 5
    warning_lines = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warning_lines == [], f"Expected no cap-related WARNING; got: {warning_lines!r}"


# ---------------------------------------------------------------------------
# Triangulate: filters combine correctly
# ---------------------------------------------------------------------------


def test_target_ip_and_keyword_filters_combine(tmp_path: Path) -> None:
    """Both filters apply: `target_ip` exact match AND `keyword` substring."""
    from nora.intervention_memory.tools import search_intervention_history
    from nora.sanitizer import Sanitizer

    settings = _settings_for(tmp_path)
    _write_record(
        tmp_path / "r1.json",
        _make_record(
            intervention_id="INT-1",
            target_ip="10.0.0.4",
            timestamp_unix=100,
            findings_and_dictamen="interference observed on channel A",
        ),
    )
    _write_record(
        tmp_path / "r2.json",
        _make_record(
            intervention_id="INT-2",
            target_ip="10.0.0.5",
            timestamp_unix=200,
            findings_and_dictamen="interference observed on channel B",
        ),
    )
    _write_record(
        tmp_path / "r3.json",
        _make_record(
            intervention_id="INT-3",
            target_ip="10.0.0.4",
            timestamp_unix=300,
            findings_and_dictamen="no anomalies at all",
        ),
    )
    sanitizer = Sanitizer()

    results = search_intervention_history(
        settings, target_ip="10.0.0.4", keyword="interference observed", sanitizer=sanitizer
    )

    assert len(results) == 1
    assert results[0]["intervention_id"] == "INT-1"


def test_ticket_number_filter_is_substring(tmp_path: Path) -> None:
    """`ticket_number` filter uses substring containment (not exact match)."""
    from nora.intervention_memory.tools import search_intervention_history
    from nora.sanitizer import Sanitizer

    settings = _settings_for(tmp_path)
    _write_record(
        tmp_path / "r1.json",
        _make_record(intervention_id="INT-1", target_ip="10.0.0.4", timestamp_unix=100, ticket_number="TKT-7400"),
    )
    _write_record(
        tmp_path / "r2.json",
        _make_record(intervention_id="INT-2", target_ip="10.0.0.5", timestamp_unix=200, ticket_number="TKT-9999"),
    )
    sanitizer = Sanitizer()

    results = search_intervention_history(settings, ticket_number="7400", sanitizer=sanitizer)

    assert len(results) == 1
    assert results[0]["ticket_number"] == "TKT-7400"


def test_stage_filter_is_case_insensitive_equality(tmp_path: Path) -> None:
    """`stage` filter is case-insensitive equality."""
    from nora.intervention_memory.tools import search_intervention_history
    from nora.sanitizer import Sanitizer

    settings = _settings_for(tmp_path)
    _write_record(
        tmp_path / "r1.json",
        _make_record(intervention_id="INT-1", target_ip="10.0.0.4", timestamp_unix=100, stage="PRE_DIAGNOSTIC"),
    )
    _write_record(
        tmp_path / "r2.json",
        _make_record(intervention_id="INT-2", target_ip="10.0.0.5", timestamp_unix=200, stage="POST_INTERVENTION"),
    )
    sanitizer = Sanitizer()

    # Lowercase stage in filter.
    results = search_intervention_history(settings, stage="pre_diagnostic", sanitizer=sanitizer)

    assert len(results) == 1
    assert results[0]["intervention_id"] == "INT-1"


# ---------------------------------------------------------------------------
# Triangulate: empty dir returns []
# ---------------------------------------------------------------------------


def test_search_with_empty_directory_returns_empty_list(tmp_path: Path) -> None:
    """Empty dir → `[]` (NOT `NO_HISTORY_FOUND`; that's the lifecycle tool's marker)."""
    from nora.intervention_memory.tools import search_intervention_history
    from nora.sanitizer import Sanitizer

    settings = _settings_for(tmp_path)

    results = search_intervention_history(settings, sanitizer=Sanitizer())

    assert results == []


def test_search_with_missing_directory_returns_empty_list(tmp_path: Path) -> None:
    """Missing dir → `[]` (storage layer does not create it)."""
    from nora.intervention_memory.tools import search_intervention_history
    from nora.sanitizer import Sanitizer

    absent_dir = tmp_path / "does-not-exist"
    settings = _settings_for(absent_dir)

    results = search_intervention_history(settings, sanitizer=Sanitizer())

    assert results == []


# ---------------------------------------------------------------------------
# R5 — get_device_lifecycle_summary
# ---------------------------------------------------------------------------


def test_lifecycle_summary_no_history_returns_status_marker(tmp_path: Path) -> None:
    """Empty dir → `{"status": "NO_HISTORY_FOUND", "target_ip": target_ip}` exactly."""
    from nora.intervention_memory.tools import get_device_lifecycle_summary
    from nora.sanitizer import Sanitizer

    settings = _settings_for(tmp_path)

    result = get_device_lifecycle_summary(settings, target_ip="10.0.0.99", sanitizer=Sanitizer())

    assert result == {"status": "NO_HISTORY_FOUND", "target_ip": "10.0.0.99"}


def test_lifecycle_summary_success_returns_all_six_fields(tmp_path: Path) -> None:
    """Two matching records → SUCCESS with all 6 fields + correct shape."""
    from nora.intervention_memory.tools import get_device_lifecycle_summary
    from nora.sanitizer import Sanitizer

    settings = _settings_for(tmp_path)
    _write_record(
        tmp_path / "r1.json",
        _make_record(
            intervention_id="INT-1",
            target_ip="10.0.0.4",
            timestamp_unix=100,
            stage="PRE_DIAGNOSTIC",
            ticket_number="TKT-7400",
        ),
    )
    _write_record(
        tmp_path / "r2.json",
        _make_record(
            intervention_id="INT-2",
            target_ip="10.0.0.4",
            timestamp_unix=200,
            stage="POST_INTERVENTION",
            ticket_number="TKT-7401",
        ),
    )
    sanitizer = Sanitizer()

    result = get_device_lifecycle_summary(settings, target_ip="10.0.0.4", sanitizer=sanitizer)

    assert result["status"] == "SUCCESS"
    assert result["target_ip"] == "10.0.0.4"
    assert result["total_recorded_interventions"] == 2
    # Stages ordered DESC by timestamp.
    assert result["stages_recorded"] == ["POST_INTERVENTION", "PRE_DIAGNOSTIC"]
    # Both tickets in the sorted list.
    assert sorted(result["associated_tickets"]) == ["TKT-7400", "TKT-7401"]
    # latest_intervention is the most recent (timestamp_unix=200).
    assert result["latest_intervention"]["timestamp_unix"] == 200
    assert result["latest_intervention"]["intervention_id"] == "INT-2"
    # All 6 keys present.
    assert set(result.keys()) == {
        "status",
        "target_ip",
        "total_recorded_interventions",
        "associated_tickets",
        "stages_recorded",
        "latest_intervention",
        "known_pre_existing_offline_subscribers",
    }


def test_known_pre_existing_offline_subscribers_from_latest_pre_diagnostic(tmp_path: Path) -> None:
    """Two PRE_DIAGNOSTIC records → newer record's subscriber list wins."""
    from nora.intervention_memory.tools import get_device_lifecycle_summary
    from nora.sanitizer import Sanitizer

    settings = _settings_for(tmp_path)
    # Older PRE_DIAGNOSTIC (timestamp 100) with subscriber luid=1.
    older = _make_record(
        intervention_id="INT-OLD",
        target_ip="10.0.0.4",
        timestamp_unix=100,
        stage="PRE_DIAGNOSTIC",
    )
    older["network_equipment"]["pre_existing_offline_subscribers"] = [
        {"luid": 1, "mac": "aa:bb:cc:dd:ee:01", "ip": "10.0.0.51", "note": "old"}
    ]
    _write_record(tmp_path / "older.json", older)
    # Newer PRE_DIAGNOSTIC (timestamp 300) with subscriber luid=7.
    newer = _make_record(
        intervention_id="INT-NEW",
        target_ip="10.0.0.4",
        timestamp_unix=300,
        stage="PRE_DIAGNOSTIC",
    )
    newer["network_equipment"]["pre_existing_offline_subscribers"] = [
        {"luid": 7, "mac": "aa:bb:cc:dd:ee:02", "ip": "10.0.0.52", "note": "newer"}
    ]
    _write_record(tmp_path / "newer.json", newer)
    sanitizer = Sanitizer()

    result = get_device_lifecycle_summary(settings, target_ip="10.0.0.4", sanitizer=sanitizer)

    assert result["status"] == "SUCCESS"
    subs = result["known_pre_existing_offline_subscribers"]
    assert len(subs) == 1
    assert subs[0]["luid"] == 7
    # The newer record's MAC literal is masked (sanitized).
    assert "aa:bb:cc:dd:ee:02" not in subs[0]["mac"]
    assert "SWITCH_ACC_" in subs[0]["mac"]