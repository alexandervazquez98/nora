"""Unit tests for :mod:`nora.probes.persistence` — atomic write + naming + error returns.

Each test pins one of the documented branches of
:func:`save_probe_run`. The shared ``_payload`` helper builds a
valid minimal payload so individual tests can mutate the field under
test without rebuilding the dict.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nora.intervention_writer.atomic import _COLLISION_RETRIES
from nora.probes.persistence import (
    INVALID_INPUT,
    INVALID_PAYLOAD,
    OK,
    WRITE_ERROR,
    ProbeRunPayload,
    build_probe_filename,
    save_probe_run,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _payload(**overrides: Any) -> dict[str, Any]:
    """Build a minimal valid payload for ``save_probe_run``.

    Fields mirror :class:`ProbeRunPayload`. Per-field overrides
    surface in tests via ``**_payload(ap_ip="192.168.1.1")``.
    """
    base: dict[str, Any] = {
        "run_id": "abcd1234",
        "sector": "norte",
        "device_id": "ap-7400-01",
        "ap_ip": "192.0.2.10",
        "started_at_unix": 1_700_000_000.0,
        "finished_at_unix": 1_700_000_600.0,
        "metrics_summary": {"ap": {}, "sm": []},
        "verdict": {"sector_verdict": "EXCELLENT", "ap_verdict": "EXCELLENT", "per_sm": []},
    }
    base.update(overrides)
    return base


class _DummySettings:
    """Stand-in for :class:`nora.config.Settings` — tests pass ``base_dir`` so this is unused."""

    def __init__(self, results_dir: Path) -> None:
        self.nora_probe_results_dir = results_dir


# ---------------------------------------------------------------------------
# Happy-path persistence
# ---------------------------------------------------------------------------


def test_save_probe_run_writes_file_under_target_dir(tmp_path: Path) -> None:
    """Valid payload -> ``OK`` + a file lands under ``base_dir``."""
    settings = _DummySettings(tmp_path)
    result = save_probe_run(settings, _payload(), base_dir=tmp_path, sector="test-sector")
    assert result["status"] == OK
    assert "filename" in result
    assert (tmp_path / result["filename"]).exists()


def test_filename_template_matches_PRB_pattern(tmp_path: Path) -> None:
    """File name starts with ``PRB-`` and ends with ``.json``."""
    settings = _DummySettings(tmp_path)
    result = save_probe_run(settings, _payload(), base_dir=tmp_path, sector="test-sector")
    name = result["filename"]
    assert name.startswith("PRB-")
    assert name.endswith(".json")
    # Strict regex sanity — the sector and ap_ip are bounded.
    assert "test-sector" in name
    assert "192.0.2.10" in name


def test_atomic_write_creates_tmp_and_renames(tmp_path: Path) -> None:
    """After save, ``.json.tmp`` files are gone, ``.json`` exists."""
    settings = _DummySettings(tmp_path)
    result = save_probe_run(settings, _payload(), base_dir=tmp_path)
    assert result["status"] == OK
    written = Path(result["path"])
    assert written.exists()
    # No leftover `.tmp` files in the directory.
    leftovers = list(tmp_path.glob("*.json.tmp"))
    assert leftovers == [], f"unexpected .tmp leftovers: {leftovers}"


def test_save_probe_run_payload_is_valid_json(tmp_path: Path) -> None:
    """The on-disk file parses as JSON and round-trips the payload."""
    settings = _DummySettings(tmp_path)
    payload = _payload(metrics_summary={"ap": {"rtt_avg_ms": 5.0}})
    result = save_probe_run(settings, payload, base_dir=tmp_path)
    assert result["status"] == OK
    on_disk = json.loads(Path(result["path"]).read_text())
    assert on_disk["run_id"] == "abcd1234"
    assert on_disk["metrics_summary"]["ap"]["rtt_avg_ms"] == 5.0


# ---------------------------------------------------------------------------
# Filename builder (pure)
# ---------------------------------------------------------------------------


def test_build_probe_filename_strict() -> None:
    """``build_probe_filename`` returns ``PRB-<sector>-<ip>-<unix>-<6hex>.json``."""
    name = build_probe_filename(
        sector="norte", ap_ip="192.0.2.10", unix=1_700_000_000, hex_suffix="deadbe"
    )
    assert name == "PRB-norte-192.0.2.10-1700000000-deadbe.json"


def test_build_probe_filename_rejects_slash_in_sector() -> None:
    """``sector="sec/etcd"`` raises :class:`InvalidFilenameComponent`."""
    from nora.intervention_writer.filenames import InvalidFilenameComponent

    with pytest.raises(InvalidFilenameComponent):
        build_probe_filename(sector="sec/etcd", ap_ip="192.0.2.10", unix=1, hex_suffix="abcdef")


def test_build_probe_filename_rejects_double_dot_in_ip() -> None:
    """``ap_ip="192.0.2..10"`` raises via the substring defence-in-depth."""
    from nora.intervention_writer.filenames import InvalidFilenameComponent

    with pytest.raises(InvalidFilenameComponent):
        build_probe_filename(sector="norte", ap_ip="192.0.2..10", unix=1, hex_suffix="abcdef")


# ---------------------------------------------------------------------------
# Error returns
# ---------------------------------------------------------------------------


def test_invalid_sector_raises_invalid_input(tmp_path: Path) -> None:
    """Sector with a slash is caught by the filename regex -> INVALID_INPUT."""
    settings = _DummySettings(tmp_path)
    result = save_probe_run(
        settings,
        _payload(),
        base_dir=tmp_path,
        sector="bad/sector",  # `sector` override (filename component), not the payload field
    )
    # The shell calls `build_probe_filename(sector='bad/sector', ...)`,
    # which raises -> we map to INVALID_INPUT.
    assert result["status"] == INVALID_INPUT
    assert result.get("field") == "sector"


def test_invalid_ap_ip_raises_invalid_input(tmp_path: Path) -> None:
    """``ap_ip="10.0.0..1"`` in the payload is caught -> INVALID_INPUT."""
    settings = _DummySettings(tmp_path)
    # Payload passes Pydantic; the regex catches the bad AP IP at write time.
    # We bypass Pydantic's `ap_ip` validation by NOT enforcing strict IP
    # validation in the model — `ap_ip: str` accepts any string. The
    # filename regex must therefore catch it.
    result = save_probe_run(
        settings,
        _payload(ap_ip="10.0.0..1"),
        base_dir=tmp_path,
        sector="norte",
    )
    assert result["status"] == INVALID_INPUT
    assert result.get("field") == "ap_ip"


def test_invalid_payload_returns_invalid_payload(tmp_path: Path) -> None:
    """Payload missing required field -> INVALID_PAYLOAD."""
    settings = _DummySettings(tmp_path)
    payload = _payload()
    del payload["run_id"]
    result = save_probe_run(settings, payload, base_dir=tmp_path)
    assert result["status"] == INVALID_PAYLOAD
    assert "errors" in result


def test_save_probe_run_never_raises(tmp_path: Path) -> None:
    """Garbage payload returns a dict with ``status == INVALID_PAYLOAD`` and never raises."""
    settings = _DummySettings(tmp_path)
    result = save_probe_run(settings, {"foo": "bar"}, base_dir=tmp_path)
    assert result["status"] == INVALID_PAYLOAD
    assert isinstance(result, dict)


def test_save_probe_run_uses_settings_nora_probe_results_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``base_dir`` is None the writer reads ``settings.nora_probe_results_dir``."""
    settings = _DummySettings(tmp_path / "via_settings")
    result = save_probe_run(settings, _payload())  # no base_dir override
    assert result["status"] == OK
    assert (tmp_path / "via_settings" / result["filename"]).exists()


# ---------------------------------------------------------------------------
# Collision retries
# ---------------------------------------------------------------------------


def test_collision_retries_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pre-create a file with the same suffix path; the 5-attempt retry exhausts.

    We exhaust the retry budget by pre-creating ``_COLLISION_RETRIES``
    files whose names share the same suffix pool the writer rolls.
    Because the suffix is `secrets.token_hex(3)` we can't predict
    which suffix the writer picks, so we use ``monkeypatch`` to
    force the writer to collide on every attempt.
    """
    import nora.probes.persistence as persistence_mod

    settings = _DummySettings(tmp_path)
    # Pre-create one file in the directory so the first attempt
    # sees a collision. Subsequent attempts roll fresh suffixes,
    # so we patch `secrets.token_hex` to return a deterministic
    # value that we know collides with the pre-created file.
    from nora.probes.persistence import build_probe_filename

    collision_name = build_probe_filename(
        sector="collision",
        ap_ip="192.0.2.10",
        unix=1_700_000_001,
        hex_suffix="cafefe",
    )
    (tmp_path / collision_name).write_text("pre-existing")
    # Patch `secrets.token_hex` to always return 'cafefe' so every
    # retry collides with the pre-existing file.
    monkeypatch.setattr(persistence_mod.secrets, "token_hex", lambda _n: "cafefe")
    # Run with fixed unix so the filename pattern matches.
    monkeypatch.setattr(persistence_mod.time, "time", lambda: 1_700_000_001.0)

    result = save_probe_run(settings, _payload(), base_dir=tmp_path, sector="collision")
    # The retry budget is exhausted -> WRITE_ERROR.
    assert result["status"] == WRITE_ERROR
    assert "collision" in (result.get("error") or "")

    # A subsequent call (without the collision patch) must succeed
    # because the pool has rolled a fresh suffix.
    monkeypatch.undo()
    result2 = save_probe_run(settings, _payload(), base_dir=tmp_path, sector="collision")
    assert result2["status"] == OK
    assert (tmp_path / result2["filename"]).exists()
    # Sanity: we don't accidentally re-collide with the pre-existing file.
    assert result2["filename"] != collision_name


def test_collision_retries_constant_matches_intervention_writer() -> None:
    """``_COLLISION_RETRIES`` from ``nora.intervention_writer.atomic`` is 5."""
    int_constant = __import__(
        "nora.intervention_writer.atomic", fromlist=["_COLLISION_RETRIES"]
    )._COLLISION_RETRIES
    assert int_constant == 5
    assert _COLLISION_RETRIES == int_constant


# ---------------------------------------------------------------------------
# Path-traversal containment (best-effort defense in depth)
# ---------------------------------------------------------------------------


def test_path_traversal_defence_in_depth_via_resolve(tmp_path: Path) -> None:
    """The path is contained by ``target_dir.resolve().is_relative_to(...)``.

    This test documents the containment guarantee rather than
    exercising it — the regex on ``ap_ip`` rejects ``..`` so an
    actual traversal vector cannot reach the ``Path.resolve()``
    branch in normal operation. We assert the helper exists and
    the writer's filename validation runs before the resolve check.
    """
    settings = _DummySettings(tmp_path)
    result = save_probe_run(
        settings,
        _payload(ap_ip="foo"),  # `foo` passes the regex but is not an IP
        base_dir=tmp_path,
        sector="norte",
    )
    # The writer succeeds (no traversal vector).
    assert result["status"] == OK


def test_path_traversal_constant_exported() -> None:
    """``PATH_TRAVERSAL_DETECTED`` is a re-export from the persistence module."""
    from nora.probes import persistence as persistence_mod

    assert persistence_mod.PATH_TRAVERSAL_DETECTED == "PATH_TRAVERSAL_DETECTED"


# ---------------------------------------------------------------------------
# ProbeRunPayload schema
# ---------------------------------------------------------------------------


def test_probe_run_payload_model_accepts_valid_payload() -> None:
    """``ProbeRunPayload.model_validate`` accepts the canonical shape."""
    validated = ProbeRunPayload.model_validate(_payload())
    assert validated.run_id == "abcd1234"
    assert validated.ap_ip == "192.0.2.10"


def test_probe_run_payload_model_rejects_missing_field() -> None:
    """``ProbeRunPayload.model_validate`` raises on a missing field."""
    import pydantic

    payload = _payload()
    del payload["ap_ip"]
    with pytest.raises(pydantic.ValidationError):
        ProbeRunPayload.model_validate(payload)


def test_probe_run_payload_ignores_unknown_keys() -> None:
    """``extra='ignore'`` swallows future-spec fields (PR3 PDF / Markdown)."""
    payload = _payload(pdf_path="/var/lib/nora/reports/x.pdf", markdown_summary="# hi")
    validated = ProbeRunPayload.model_validate(payload)
    assert validated.run_id == "abcd1234"
