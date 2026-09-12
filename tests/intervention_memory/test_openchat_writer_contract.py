"""OpenChat writer ↔ NORA reader contract regression test.

Locks in the field-name convention that openchat's deploy scripts MUST
follow when writing POST_MIGRATION_VERIFIED records. Background:

- NORA's `correlate_sector_interference` reads the canonical field
  `network_equipment.carrier_frequency_mhz` (`src/nora/intervention_memory/
  models.py:74`).
- Openchat's `cambium_pmp450_tool._auto_save_memory` historically wrote
  only `verified_carrier_frequency_mhz` for POST_MIGRATION_VERIFIED
  records, leaving correlate to silently skip them.
- The fix landed in openchat's `deploy_v7_intervention_memory.py` (line
  ~1024) and `deploy_v8_unbiased_pre_report.py` (line ~1109): the
  `eq_info` dict for POST_MIGRATION_VERIFIED now ALSO includes
  `carrier_frequency_mhz: ap_freq_verified`.

This test pins that contract so the gap cannot return.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from nora.intervention_memory.shim_webui import Tools
from nora.intervention_memory.tools import correlate_sector_interference
from nora.sanitizer import Sanitizer


def _make_post_migration_record(
    *,
    intervention_id_suffix: str,
    target_ip: str,
    tower: str,
    target_freq_mhz: float,
    include_canonical_field: bool,
    timestamp_unix: int,
) -> dict:
    """Build a POST_MIGRATION_VERIFIED record matching openchat's shape.

    Mirrors the JSON written by openchat's deploy_v7/v8 `_auto_save_memory`.
    """
    ne: dict = {
        "target_ip": target_ip,
        "system_name": tower,
        "previous_frequency_mhz": 5760.0,
        "verified_carrier_frequency_mhz": target_freq_mhz,
        "active_sms": 5,
        "initial_sms": 5,
    }
    if include_canonical_field:
        ne["carrier_frequency_mhz"] = target_freq_mhz
    return {
        "intervention_id": f"INT-{intervention_id_suffix}-{target_ip}-{timestamp_unix}",
        "timestamp_iso": "2026-09-11T15:00:00Z",
        "timestamp_unix": timestamp_unix,
        "ticket_number": "CONTRACT",
        "target_ip": target_ip,
        "stage": "POST_MIGRATION_VERIFIED",
        "record_name": f"Migración verificada en {tower}",
        "status": "COMPLETED",
        "agent_name": "Cambium Hardware Engine (Auto-Log)",
        "network_equipment": ne,
        "findings_and_dictamen": "contract regression record",
        "created_at": "2026-09-11T15:00:00Z",
    }


@pytest.fixture
def interventions_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Hermetic interventions dir wired through env + shim constant."""
    p = tmp_path / "interventions"
    p.mkdir()
    monkeypatch.setenv("NORA_INTERVENTIONS_DIR", str(p))
    # The shim caches the dir via Tools.INTERVENTIONS_DIR; mirror the env
    # so shim-based callers point at the same hermetic dir.
    Tools.INTERVENTIONS_DIR = str(p)
    yield p
    Tools.INTERVENTIONS_DIR = ""


def test_post_migration_without_canonical_field_is_skipped(
    interventions_dir: Path,
) -> None:
    """BEFORE-fix shape: POST_MIGRATION_VERIFIED missing `carrier_frequency_mhz`.

    `correlate_sector_interference` MUST return 0 conflicts. This is the
    bug shape — if a future openchat deploy script regresses and drops
    the canonical field again, this assertion catches it.
    """
    from nora.config import Settings

    base = int(time.time())
    record = _make_post_migration_record(
        intervention_id_suffix="OLD",
        target_ip="10.99.0.1",
        tower="TWR-CONTRACT-A",
        target_freq_mhz=5805.0,
        include_canonical_field=False,
        timestamp_unix=base,
    )
    fp = interventions_dir / f"CONTRACT_10-99-0-1_POST_MIGRATION_VERIFIED_{base}.json"
    fp.write_text(json.dumps(record, indent=2), encoding="utf-8")

    settings = Settings()
    result = correlate_sector_interference(
        settings=settings,
        tower_name="TWR-CONTRACT-A",
        target_frequency_mhz=5805.0,
        channel_width_mhz=20.0,
        sanitizer=Sanitizer(),
    )
    assert result["detected_conflicts"] == [], (
        f"Expected 0 conflicts (legacy shape without carrier_frequency_mhz); "
        f"got {result['detected_conflicts']!r}"
    )


def test_post_migration_with_canonical_field_is_detected(
    interventions_dir: Path,
) -> None:
    """AFTER-fix shape: POST_MIGRATION_VERIFIED WITH `carrier_frequency_mhz`.

    `correlate_sector_interference` MUST return exactly 1 conflict
    classified `CO_CHANNEL` (delta 0 MHz). This is the fixed shape that
    openchat's deploy_v7/v8 now produce.
    """
    from nora.config import Settings

    base = int(time.time()) + 1  # distinct from the BEFORE test
    record = _make_post_migration_record(
        intervention_id_suffix="NEW",
        target_ip="10.99.0.2",
        tower="TWR-CONTRACT-B",
        target_freq_mhz=5805.0,
        include_canonical_field=True,
        timestamp_unix=base,
    )
    fp = interventions_dir / f"CONTRACT_10-99-0-2_POST_MIGRATION_VERIFIED_{base}.json"
    fp.write_text(json.dumps(record, indent=2), encoding="utf-8")

    settings = Settings()
    result = correlate_sector_interference(
        settings=settings,
        tower_name="TWR-CONTRACT-B",
        target_frequency_mhz=5805.0,
        channel_width_mhz=20.0,
        sanitizer=Sanitizer(),
    )
    assert len(result["detected_conflicts"]) == 1, (
        f"Expected 1 conflict (canonical field present); got {result['detected_conflicts']!r}"
    )
    conflict = result["detected_conflicts"][0]
    assert conflict["carrier_frequency_mhz"] == 5805.0
    assert conflict["frequency_delta_mhz"] == 0.0
    assert conflict["potential_conflict"] == "CO_CHANNEL"
    assert conflict["neighbor_ip"] == "10.99.0.2"
