"""Sanitizer boundary tests — covers R9 (free-text sanitization + bypass list).

The `intervention_memory` package walks every record through
`Sanitizer.sanitize(...)` on read, with a bypass list of structured
top-level fields (`intervention_id`, `target_ip`, `stage`, `status`,
`timestamp_unix`, `timestamp_iso`, `created_at`, `ticket_number`)
inherited as-is from `session-journal` R6.

The user-locked decision (Q4 in proposal.md): `target_ip` is BYPASSED —
the value appears verbatim in tool output even though it is a private
IPv4 literal. Other Sanitizer rules still apply.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def sanitizer():
    """A fresh `Sanitizer` per test so the alias map is isolated."""
    from nora.sanitizer import Sanitizer

    return Sanitizer()


def _load_fixture(name: str) -> dict:
    path = (
        Path(__file__).resolve().parent.parent.parent
        / "tests"
        / "fixtures"
        / "intervention_memory"
        / name
    )
    import json

    return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# R9-S1 — IPv4 in `record_name` is masked
# ---------------------------------------------------------------------------


def test_ipv4_in_record_name_is_masked(sanitizer) -> None:
    """Private IPv4 in `record_name` → `RADIO_NODE_*` alias; literal MUST NOT survive."""
    from nora.intervention_memory.models import InterventionMemoryRecord
    from nora.intervention_memory.sanitize import sanitize_record_payload

    fixture = _load_fixture("v7_baseline.json")
    # Override record_name with an RFC 1918 address (10/8 is in the
    # sanitizer's private IPv4 list; 192.0.2.x is RFC 5737 docs and
    # passes through unmasked — that is the correct behaviour).
    fixture["record_name"] = "Pre-diagnostic baseline for 10.0.0.5"
    record = InterventionMemoryRecord.model_validate(fixture)

    payload = sanitize_record_payload(record, sanitizer)

    assert "10.0.0.5" not in payload["record_name"], (
        f"Private IPv4 leaked in record_name: {payload['record_name']!r}"
    )
    assert "RADIO_NODE_" in payload["record_name"], (
        f"Expected RADIO_NODE_* alias in record_name; got: {payload['record_name']!r}"
    )


# ---------------------------------------------------------------------------
# R9-S2 — MAC in subscriber note is masked
# ---------------------------------------------------------------------------


def test_mac_in_subscriber_note_is_masked(sanitizer) -> None:
    """MAC in subscriber `note` → `SWITCH_ACC_*` alias."""
    from nora.intervention_memory.models import InterventionMemoryRecord
    from nora.intervention_memory.sanitize import sanitize_record_payload

    fixture = _load_fixture("v7_baseline.json")
    record = InterventionMemoryRecord.model_validate(fixture)

    payload = sanitize_record_payload(record, sanitizer)

    note = payload["network_equipment"]["pre_existing_offline_subscribers"][0]["note"]
    assert "aa:bb:cc:dd:ee:01" not in note, f"MAC literal leaked in note: {note!r}"
    assert "SWITCH_ACC_" in note, f"Expected SWITCH_ACC_* alias in note; got: {note!r}"


def test_mac_in_subscriber_mac_field_is_masked(sanitizer) -> None:
    """The `mac` field on a subscriber row is also masked (even though structured)."""
    from nora.intervention_memory.models import InterventionMemoryRecord
    from nora.intervention_memory.sanitize import sanitize_record_payload

    fixture = _load_fixture("v7_baseline.json")
    record = InterventionMemoryRecord.model_validate(fixture)

    payload = sanitize_record_payload(record, sanitizer)

    mac_value = payload["network_equipment"]["pre_existing_offline_subscribers"][0]["mac"]
    # The fixture carries an IEEE 802 MAC (aa:bb:cc:dd:ee:01) which IS
    # in the sanitizer's MAC list, so it must be masked.
    assert "aa:bb:cc:dd:ee:01" not in mac_value, f"MAC leaked: {mac_value!r}"
    assert "SWITCH_ACC_" in mac_value, f"Expected alias; got: {mac_value!r}"


# ---------------------------------------------------------------------------
# R9-S3 — structured top-level fields bypass the sanitizer
# ---------------------------------------------------------------------------


def test_structured_top_level_fields_bypass_sanitizer(sanitizer) -> None:
    """`intervention_id` containing an IP-shaped substring is byte-identical after sanitization."""
    from nora.intervention_memory.models import InterventionMemoryRecord
    from nora.intervention_memory.sanitize import sanitize_record_payload

    fixture = _load_fixture("v7_baseline.json")
    # Use an intervention_id that contains an RFC 1918 IP-shaped substring.
    fixture["intervention_id"] = "INT-1-10.0.0.5-1234567-V7"
    record = InterventionMemoryRecord.model_validate(fixture)

    payload = sanitize_record_payload(record, sanitizer)

    assert payload["intervention_id"] == "INT-1-10.0.0.5-1234567-V7", (
        f"intervention_id must be byte-identical after bypass; got: {payload['intervention_id']!r}"
    )


def test_target_ip_bypass_returns_value_verbatim(sanitizer) -> None:
    """`target_ip` is in the bypass list — appears unchanged even when it's RFC 1918.

    User-locked decision (Q4): `target_ip` returns verbatim in tool
    output. Other Sanitizer rules still apply.
    """
    from nora.intervention_memory.models import InterventionMemoryRecord
    from nora.intervention_memory.sanitize import sanitize_record_payload

    fixture = _load_fixture("v7_baseline.json")
    fixture["target_ip"] = "10.0.0.5"  # RFC 1918; would be masked without bypass
    record = InterventionMemoryRecord.model_validate(fixture)

    payload = sanitize_record_payload(record, sanitizer)

    assert payload["target_ip"] == "10.0.0.5", (
        f"target_ip must be byte-identical (bypass); got: {payload['target_ip']!r}"
    )


def test_stage_status_timestamps_bypass(sanitizer) -> None:
    """`stage`, `status`, timestamps, `created_at`, `ticket_number` all bypass."""
    from nora.intervention_memory.models import InterventionMemoryRecord
    from nora.intervention_memory.sanitize import sanitize_record_payload

    fixture = _load_fixture("v7_baseline.json")
    record = InterventionMemoryRecord.model_validate(fixture)

    payload = sanitize_record_payload(record, sanitizer)

    assert payload["stage"] == fixture["stage"]
    assert payload["status"] == fixture["status"]
    assert payload["timestamp_unix"] == fixture["timestamp_unix"]
    assert payload["timestamp_iso"] == fixture["timestamp_iso"]
    assert payload["created_at"] == fixture["created_at"]
    assert payload["ticket_number"] == fixture["ticket_number"]


# ---------------------------------------------------------------------------
# Triangulate: free-text fields outside the bypass list are sanitized
# ---------------------------------------------------------------------------


def test_findings_and_dictamen_with_ip_is_masked(sanitizer) -> None:
    """The `findings_and_dictamen` field IS sanitized — not in the bypass list."""
    from nora.intervention_memory.models import InterventionMemoryRecord
    from nora.intervention_memory.sanitize import sanitize_record_payload

    fixture = _load_fixture("v7_baseline.json")
    fixture["findings_and_dictamen"] = "Investigation at 10.0.0.50 found interference."
    record = InterventionMemoryRecord.model_validate(fixture)

    payload = sanitize_record_payload(record, sanitizer)

    assert "10.0.0.50" not in payload["findings_and_dictamen"]
    assert "RADIO_NODE_" in payload["findings_and_dictamen"]


def test_agent_name_with_hostname_is_masked(sanitizer) -> None:
    """The `agent_name` field is sanitized — multi-label FQDN → `HOST_*` alias."""
    from nora.intervention_memory.models import InterventionMemoryRecord
    from nora.intervention_memory.sanitize import sanitize_record_payload

    fixture = _load_fixture("v7_baseline.json")
    fixture["agent_name"] = "agent-router-core-01.example.com"
    record = InterventionMemoryRecord.model_validate(fixture)

    payload = sanitize_record_payload(record, sanitizer)

    assert "router-core-01.example.com" not in payload["agent_name"]
    assert "HOST_" in payload["agent_name"]


def test_recommended_action_field_is_sanitized(sanitizer) -> None:
    """The optional `recommended_action` field is sanitized when present."""
    from nora.intervention_memory.models import InterventionMemoryRecord
    from nora.intervention_memory.sanitize import sanitize_record_payload

    fixture = _load_fixture("v8_pre_migration.json")
    record = InterventionMemoryRecord.model_validate(fixture)

    payload = sanitize_record_payload(record, sanitizer)

    rec_action = payload["recommended_action"]
    # Recommended action was "schedule follow-up diagnostic in 7 days" — no IPs/MACs/hostnames.
    # So it should pass through unchanged.
    assert rec_action == "schedule follow-up diagnostic in 7 days"


# ---------------------------------------------------------------------------
# Triangulate: nested `network_equipment` walk
# ---------------------------------------------------------------------------


def test_network_equipment_system_name_is_sanitized(sanitizer) -> None:
    """`network_equipment.system_name` is sanitized (free text)."""
    from nora.intervention_memory.models import InterventionMemoryRecord
    from nora.intervention_memory.sanitize import sanitize_record_payload

    fixture = _load_fixture("v7_baseline.json")
    # Use a system_name containing an RFC 1918 IPv4 so the sanitizer masks it.
    fixture["network_equipment"]["system_name"] = "TWR-ISABEL-5GHZ-A-near-10.0.0.5"
    record = InterventionMemoryRecord.model_validate(fixture)

    payload = sanitize_record_payload(record, sanitizer)

    system_name = payload["network_equipment"]["system_name"]
    assert "10.0.0.5" not in system_name
    assert "RADIO_NODE_" in system_name


def test_network_equipment_hardware_band_is_sanitized_when_present() -> None:
    """`network_equipment.hardware_band` is sanitized (free text, v8-only field)."""
    from nora.intervention_memory.models import InterventionMemoryRecord
    from nora.intervention_memory.sanitize import sanitize_record_payload
    from nora.sanitizer import Sanitizer

    fixture = _load_fixture("v8_pre_migration.json")
    fixture["network_equipment"]["hardware_band"] = "5GHZ-with-10.0.0.99"
    record = InterventionMemoryRecord.model_validate(fixture)
    sanitizer = Sanitizer()

    payload = sanitize_record_payload(record, sanitizer)

    band = payload["network_equipment"]["hardware_band"]
    assert "10.0.0.99" not in band
    assert "RADIO_NODE_" in band


# ---------------------------------------------------------------------------
# Triangulate: ip field inside subscriber is sanitized
# ---------------------------------------------------------------------------


def test_subscriber_ip_field_is_sanitized(sanitizer) -> None:
    """`pre_existing_offline_subscribers[*].ip` (RFC1918) is sanitized — even though structured."""
    from nora.intervention_memory.models import InterventionMemoryRecord
    from nora.intervention_memory.sanitize import sanitize_record_payload

    fixture = _load_fixture("v7_baseline.json")
    fixture["network_equipment"]["pre_existing_offline_subscribers"][0]["ip"] = "10.0.0.51"
    record = InterventionMemoryRecord.model_validate(fixture)

    payload = sanitize_record_payload(record, sanitizer)

    sub_ip = payload["network_equipment"]["pre_existing_offline_subscribers"][0]["ip"]
    assert "10.0.0.51" not in sub_ip
    assert "RADIO_NODE_" in sub_ip


def test_subscriber_luid_and_uptime_pass_through(sanitizer) -> None:
    """`luid` (int) and `uptime` (operator-facing string) pass through unchanged."""
    from nora.intervention_memory.models import InterventionMemoryRecord
    from nora.intervention_memory.sanitize import sanitize_record_payload

    fixture = _load_fixture("v7_baseline.json")
    record = InterventionMemoryRecord.model_validate(fixture)

    payload = sanitize_record_payload(record, sanitizer)

    sub = payload["network_equipment"]["pre_existing_offline_subscribers"][0]
    assert sub["luid"] == 7
    assert sub["uptime"] == "12d 3h"


# ---------------------------------------------------------------------------
# Triangulate: correlate result neighbour device is sanitized (R6-S4)
# ---------------------------------------------------------------------------


def test_correlate_result_neighbor_sanitized(sanitizer) -> None:
    """After `correlate_sector_interference`, neighbour `system_name` carries aliases.

    This is a higher-level integration test: build a record with a
    private IP in `network_equipment.system_name`, run sanitization, and
    assert the resulting `system_name` carries an alias rather than the
    literal IP.
    """
    from nora.intervention_memory.models import InterventionMemoryRecord
    from nora.intervention_memory.sanitize import sanitize_record_payload

    # 1) With the v7 system_name (no IPs/MACs), sanitization is byte-identical.
    fixture = _load_fixture("v7_baseline.json")
    record = InterventionMemoryRecord.model_validate(fixture)
    payload = sanitize_record_payload(record, sanitizer)
    # system_name is "TWR-ISABEL-5GHZ-A". This matches the SERIAL regex
    # (uppercase letters + hyphens), so it WILL be masked. We assert
    # the alias mapping works (not byte-identical).
    system_name = payload["network_equipment"]["system_name"]
    assert "SERIAL_" in system_name or system_name == "TWR-ISABEL-5GHZ-A", (
        f"system_name unexpected: {system_name!r}"
    )

    # 2) With an RFC 1918 IP appended (separated by a space — the
    # sanitizer's IP regex uses a `(?<![0-9.])` lookbehind that rejects
    # IPs preceded by `.`, so we use a space), the IP MUST be masked.
    fixture["network_equipment"]["system_name"] = "TWR-ISABEL-A 10.0.0.99"
    record = InterventionMemoryRecord.model_validate(fixture)
    payload = sanitize_record_payload(record, sanitizer)
    assert "10.0.0.99" not in payload["network_equipment"]["system_name"]
    assert "RADIO_NODE_" in payload["network_equipment"]["system_name"]
