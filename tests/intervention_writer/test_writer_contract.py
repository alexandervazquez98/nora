"""Schema + sanitisation tests for the writer contract (W4 + W5).

These tests pin the writer's behaviour at the Pydantic schema boundary
and the sanitisation walker:

- A valid payload → `OK` with the `intervention_id` stem.
- An invalid `stage` literal → `INVALID_PAYLOAD`, no disk write.
- A free-text field containing a private IPv4 literal lands as the
  alias (`RADIO_NODE_*`), not the literal IP, on disk (W4).
- Bypass scalars (`ticket_number`, `target_ip`, etc.) survive the
  sanitiser byte-identical (R9).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nora.config import Settings
from nora.intervention_writer.writer import save_intervention_record


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        _env_file_encoding=None,
        nora_interventions_dir=tmp_path,
    )


def _valid_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "intervention_id": "INT-TKT-001-10.0.0.1-1700000000-a1b2c3",
        "timestamp_iso": "2026-01-01T12:00:00+00:00",
        "timestamp_unix": 1700000000,
        "ticket_number": "TKT-001",
        "target_ip": "10.0.0.1",
        "stage": "PRE_DIAGNOSTIC",
        "record_name": "Investigating",
        "status": "COMPLETED",
        "agent_name": "test-agent",
        "findings_and_dictamen": "no findings",
        "created_at": "2026-01-01T12:00:00+00:00",
        "network_equipment": {},
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# W5 — schema validation
# ---------------------------------------------------------------------------


def test_valid_payload_returns_ok_and_writes_one_file(tmp_path: Path) -> None:
    """A schema-valid payload lands one `.json` and the response names the stem."""
    settings = _settings(tmp_path)
    result = save_intervention_record(settings, _valid_payload())

    assert result["status"] == "OK", f"Expected OK; got: {result}"
    assert "intervention_id" in result, (
        f"Response must include `intervention_id` (filename stem); got: {result}"
    )
    # Stem is the filename minus `.json`.
    assert result["intervention_id"].startswith("INT-TKT-001-10.0.0.1-"), (
        f"`intervention_id` stem must match template; got: {result['intervention_id']!r}"
    )

    files = list(tmp_path.iterdir())
    assert len(files) == 1, f"Expected one .json on disk; got: {files}"
    assert files[0].name.endswith(".json")


def test_invalid_stage_returns_invalid_payload(tmp_path: Path) -> None:
    """`stage="PRE_MIGRATIO"` (typo) → `INVALID_PAYLOAD`, no disk write."""
    settings = _settings(tmp_path)
    result = save_intervention_record(settings, _valid_payload(stage="PRE_MIGRATIO"))
    assert result["status"] == "INVALID_PAYLOAD", (
        f"Expected INVALID_PAYLOAD on typo'd stage; got: {result}"
    )
    # No disk write on validation failure.
    assert list(tmp_path.iterdir()) == [], (
        f"Writer must NOT land a file on validation failure; got: {list(tmp_path.iterdir())}"
    )


def test_missing_required_field_returns_invalid_payload(tmp_path: Path) -> None:
    """Dropping `findings_and_dictamen` (required) → `INVALID_PAYLOAD`."""
    payload = _valid_payload()
    payload.pop("findings_and_dictamen")
    settings = _settings(tmp_path)
    result = save_intervention_record(settings, payload)
    assert result["status"] == "INVALID_PAYLOAD", (
        f"Expected INVALID_PAYLOAD on missing required field; got: {result}"
    )
    assert list(tmp_path.iterdir()) == []


def test_invalid_payload_includes_error_structure(tmp_path: Path) -> None:
    """`INVALID_PAYLOAD` returns a sanitised `errors` list with loc + type."""
    settings = _settings(tmp_path)
    result = save_intervention_record(settings, _valid_payload(stage="NOT_A_STAGE"))
    assert result["status"] == "INVALID_PAYLOAD"
    assert "errors" in result, f"INVALID_PAYLOAD must include `errors`; got: {result}"
    assert isinstance(result["errors"], list)
    assert len(result["errors"]) >= 1
    first = result["errors"][0]
    assert "loc" in first
    assert "type" in first
    # No raw Pydantic `ctx` / `input` / `url` — those can leak schema paths.
    assert "ctx" not in first, f"Error must NOT include raw Pydantic ctx; got: {first}"
    assert "input" not in first, f"Error must NOT echo the input; got: {first}"


# ---------------------------------------------------------------------------
# W4 — sanitization on write
# ---------------------------------------------------------------------------


def test_free_text_ipv4_literal_is_masked_on_disk(tmp_path: Path) -> None:
    """A private IPv4 inside `findings_and_dictamen` lands as `RADIO_NODE_*`."""
    settings = _settings(tmp_path)
    payload = _valid_payload(findings_and_dictamen="node 192.168.1.1 was down")
    result = save_intervention_record(settings, payload)
    assert result["status"] == "OK"

    on_disk_path = next(tmp_path.iterdir())
    on_disk = json.loads(on_disk_path.read_text())
    assert "192.168.1.1" not in on_disk["findings_and_dictamen"], (
        f"Private IP must NOT appear on disk; got: {on_disk['findings_and_dictamen']!r}"
    )
    assert "RADIO_NODE_" in on_disk["findings_and_dictamen"], (
        f"Alias must appear on disk; got: {on_disk['findings_and_dictamen']!r}"
    )


def test_record_name_ipv4_literal_is_masked_on_disk(tmp_path: Path) -> None:
    """A private IPv4 inside `record_name` (also free text) is masked."""
    settings = _settings(tmp_path)
    payload = _valid_payload(record_name="Investigating 10.0.0.5 today")
    save_intervention_record(settings, payload)
    on_disk = json.loads(next(tmp_path.iterdir()).read_text())
    assert "10.0.0.5" not in on_disk["record_name"], (
        f"Private IP must NOT appear in record_name; got: {on_disk['record_name']!r}"
    )
    assert "RADIO_NODE_" in on_disk["record_name"]


def test_bypass_fields_survive_byte_identical(tmp_path: Path) -> None:
    """Bypass scalars (`ticket_number`, `target_ip`, etc.) are unchanged on disk.

    The on-disk file's `ticket_number` is byte-identical to the input.
    `target_ip` is also a bypass field (R9) so the IP literal in the
    canonical field is preserved verbatim — but the free-text fields
    above it are masked.
    """
    settings = _settings(tmp_path)
    payload = _valid_payload(
        ticket_number="TKT-7400",
        target_ip="10.0.0.5",
        stage="PRE_DIAGNOSTIC",
        status="COMPLETED",
        findings_and_dictamen="no private IP here",
    )
    save_intervention_record(settings, payload)
    on_disk = json.loads(next(tmp_path.iterdir()).read_text())

    assert on_disk["ticket_number"] == "TKT-7400", (
        f"ticket_number must be byte-identical; got: {on_disk['ticket_number']!r}"
    )
    assert on_disk["target_ip"] == "10.0.0.5", (
        f"target_ip must be byte-identical (R9 bypass); got: {on_disk['target_ip']!r}"
    )
    assert on_disk["stage"] == "PRE_DIAGNOSTIC"
    assert on_disk["status"] == "COMPLETED"


def test_on_disk_file_is_self_masked_end_to_end(tmp_path: Path) -> None:
    """The entire on-disk JSON contains no private IPv4 / MAC / hostname literal.

    Pasting credentials into `findings_and_dictamen` lands masked on
    disk (defense in depth). A future reader that forgets to sanitize
    still gets a safe file.
    """
    settings = _settings(tmp_path)
    payload = _valid_payload(
        findings_and_dictamen=(
            "node 192.168.1.1 / mac aa:bb:cc:dd:ee:ff / "
            "router-core-01.example.com / serial ABC123XYZ-PROD-001"
        ),
    )
    save_intervention_record(settings, payload)
    on_disk_text = next(tmp_path.iterdir()).read_text()

    forbidden_literals = (
        "192.168.1.1",
        "aa:bb:cc:dd:ee:ff",
        "router-core-01.example.com",
        "ABC123XYZ-PROD-001",
    )
    for literal in forbidden_literals:
        assert literal not in on_disk_text, (
            f"Forbidden literal {literal!r} must NOT appear on disk; on-disk: {on_disk_text!r}"
        )
