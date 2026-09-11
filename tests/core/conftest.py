"""Shared fixtures for `tests/core/` — session-journal package tests.

The session-journal package owns one per-test journal directory, a hermetic
Settings instance bound to that directory, and a fresh Sanitizer. Tests must
never write to a real `./var/sessions/` directory; every test gets `tmp_path`.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def journal_dir(tmp_path: Path) -> Path:
    """An empty, writable journal directory unique to this test."""
    d = tmp_path / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def fresh_sanitizer():
    """A brand-new `Sanitizer` — its alias map is empty."""
    from nora.sanitizer import Sanitizer

    return Sanitizer()
