"""Pydantic model tests — covers R1 (v7 + v8 forward/backward tolerance).

Three RED tests drive the model design:

1. `test_v7_record_parses_without_recommended_action` — v7 records lack
   `recommended_action`; the field defaults to `None` and parsing does
   not raise.
2. `test_v8_record_with_mac_address_alias_is_tolerated` — v8 records
   write `mac_address` (not the canonical `mac`); the alias is silently
   dropped via `extra="ignore"` and `record.mac is None`.
3. `test_invalid_stage_literal_raises_validation_error` — a bogus
   `stage` value MUST raise `pydantic.ValidationError`; the
   `Literal[...]` constraint catches the typo at the parse boundary.

Each test pins one scenario from `specs/intervention-memory/spec.md` R1.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "tests" / "fixtures" / "intervention_memory"


def _load_fixture(name: str) -> dict:
    """Load a JSON fixture from `tests/fixtures/intervention_memory/`."""
    path = FIXTURES_DIR / name
    return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# R1-S1 — v7 record without `recommended_action` parses
# ---------------------------------------------------------------------------


def test_v7_record_parses_without_recommended_action() -> None:
    """v7 fixture: no `recommended_action` key → field defaults to None, no raise."""
    from nora.intervention_memory.models import InterventionMemoryRecord

    fixture = _load_fixture("v7_baseline.json")
    assert "recommended_action" not in fixture, (
        "v7_baseline.json fixture must omit `recommended_action` to drive R1-S1"
    )

    record = InterventionMemoryRecord.model_validate(fixture)

    assert record.recommended_action is None, (
        f"v7 record should default recommended_action to None; got: {record.recommended_action!r}"
    )


def test_v7_record_exposes_required_top_level_fields() -> None:
    """v7 fixture parses; every required top-level field is populated."""
    from nora.intervention_memory.models import InterventionMemoryRecord

    fixture = _load_fixture("v7_baseline.json")
    record = InterventionMemoryRecord.model_validate(fixture)

    assert record.intervention_id
    assert record.target_ip
    assert record.timestamp_unix == fixture["timestamp_unix"]
    assert record.stage == fixture["stage"]
    assert record.status == fixture["status"]
    assert record.ticket_number == fixture["ticket_number"]
    assert record.record_name == fixture["record_name"]


# ---------------------------------------------------------------------------
# R1-S2 — v8 record with `mac_address` alias is tolerated
# ---------------------------------------------------------------------------


def test_v8_record_with_mac_address_alias_is_tolerated() -> None:
    """v8 fixture: `mac_address` (legacy alias) is silently dropped; no raise, `record.mac is None`."""
    from nora.intervention_memory.models import InterventionMemoryRecord

    fixture = _load_fixture("v8_pre_migration.json")
    subscriber = fixture["network_equipment"]["pre_existing_offline_subscribers"][0]
    assert "mac_address" in subscriber, (
        "v8_pre_migration.json fixture must carry mac_address to drive R1-S2"
    )

    record = InterventionMemoryRecord.model_validate(fixture)

    # No exception raised by extra="ignore"; mac (canonical) stays None.
    assert record.network_equipment.pre_existing_offline_subscribers[0].mac is None


def test_v8_record_with_mac_address_field_does_not_promote_into_mac() -> None:
    """The `mac_address` alias is NOT promoted into the canonical `mac` field.

    Future migrations are explicitly out of scope for this slice (per
    spec §5). The alias is silently dropped. The first subscriber carries
    `mac_address` (legacy alias); its canonical `mac` MUST stay None.
    Other subscribers (canonical `mac`) are populated normally.
    """
    from nora.intervention_memory.models import InterventionMemoryRecord

    fixture = _load_fixture("v8_pre_migration.json")
    record = InterventionMemoryRecord.model_validate(fixture)

    subs = record.network_equipment.pre_existing_offline_subscribers
    # First subscriber has `mac_address` (legacy alias) → canonical `mac` is None.
    assert subs[0].mac is None, (
        f"mac_address alias should NOT populate canonical mac; got: {subs[0].mac!r}"
    )
    # Second subscriber has canonical `mac` → populated as-is.
    assert subs[1].mac == "aa:bb:cc:dd:ee:03"


# ---------------------------------------------------------------------------
# R1-S3 — invalid stage literal raises ValidationError
# ---------------------------------------------------------------------------


def test_invalid_stage_literal_raises_validation_error() -> None:
    """`stage = "BOGUS_STAGE"` MUST raise `ValidationError` (Literal constraint)."""
    from nora.intervention_memory.models import InterventionMemoryRecord

    fixture = _load_fixture("v7_baseline.json")
    bad = {**fixture, "stage": "BOGUS_STAGE"}

    with pytest.raises(ValidationError) as excinfo:
        InterventionMemoryRecord.model_validate(bad)
    assert "stage" in str(excinfo.value).lower(), (
        f"Validation error should name `stage`; got: {excinfo.value}"
    )


def test_invalid_status_literal_raises_validation_error() -> None:
    """`status = "MUTED"` is rejected; only the four defined literals are accepted."""
    from nora.intervention_memory.models import InterventionMemoryRecord

    fixture = _load_fixture("v7_baseline.json")
    bad = {**fixture, "status": "MUTED"}

    with pytest.raises(ValidationError) as excinfo:
        InterventionMemoryRecord.model_validate(bad)
    assert "status" in str(excinfo.value).lower()


# ---------------------------------------------------------------------------
# Triangulate: extra keys are silently dropped (extra="ignore" contract)
# ---------------------------------------------------------------------------


def test_extra_top_level_keys_are_ignored() -> None:
    """Unknown top-level keys (e.g., future v9 fields) are silently ignored."""
    from nora.intervention_memory.models import InterventionMemoryRecord

    fixture = _load_fixture("v7_baseline.json")
    extended = {**fixture, "future_v9_field": "ignore-me", "another_unknown": 42}

    record = InterventionMemoryRecord.model_validate(extended)
    # No exception, record is constructed cleanly.
    assert record.intervention_id == fixture["intervention_id"]


def test_extra_nested_keys_are_ignored() -> None:
    """Unknown keys nested inside `network_equipment` are silently ignored."""
    from nora.intervention_memory.models import InterventionMemoryRecord

    fixture = _load_fixture("v7_baseline.json")
    fixture["network_equipment"]["future_nested_field"] = {"anything": True}

    record = InterventionMemoryRecord.model_validate(fixture)
    assert record.network_equipment.system_name == fixture["network_equipment"]["system_name"]


# ---------------------------------------------------------------------------
# Triangulate: required fields are enforced
# ---------------------------------------------------------------------------


def test_missing_required_target_ip_raises_validation_error() -> None:
    """Removing a required field raises `ValidationError`."""
    from nora.intervention_memory.models import InterventionMemoryRecord

    fixture = _load_fixture("v7_baseline.json")
    del fixture["target_ip"]

    with pytest.raises(ValidationError) as excinfo:
        InterventionMemoryRecord.model_validate(fixture)
    assert "target_ip" in str(excinfo.value)


def test_network_equipment_optional_fields_default_none() -> None:
    """Every Optional field on `NetworkEquipmentBlock` defaults to None when missing."""
    from nora.intervention_memory.models import InterventionMemoryRecord

    fixture = _load_fixture("v7_baseline.json")
    fixture["network_equipment"] = {}  # wipe everything

    record = InterventionMemoryRecord.model_validate(fixture)
    ne = record.network_equipment
    assert ne.system_name is None
    assert ne.hardware_band is None
    assert ne.carrier_frequency_mhz is None
    assert ne.total_provisioned_sms is None
    assert ne.active_online_sms_count is None
    assert ne.pre_existing_offline_sms_count is None
    assert ne.frame_utilization_dl_pct is None
    assert ne.frame_utilization_ul_pct is None
    assert ne.pre_existing_offline_subscribers == []


# ---------------------------------------------------------------------------
# Triangulate: round-trip via model_dump preserves sanitization eligibility
# ---------------------------------------------------------------------------


def test_model_dump_json_mode_is_json_serialisable() -> None:
    """`model_dump(mode="json")` returns a JSON-safe dict (no datetime objects)."""
    from nora.intervention_memory.models import InterventionMemoryRecord

    fixture = _load_fixture("v7_baseline.json")
    record = InterventionMemoryRecord.model_validate(fixture)

    dumped = record.model_dump(mode="json")
    # Must be re-serialisable to JSON without further conversion.
    serialised = json.dumps(dumped)
    reloaded = json.loads(serialised)
    assert reloaded["intervention_id"] == fixture["intervention_id"]
    assert reloaded["timestamp_unix"] == fixture["timestamp_unix"]
    assert reloaded["stage"] == fixture["stage"]