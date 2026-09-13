"""Filename builder tests — W1 + W2 of the writer contract.

Covers the regex-driven sanitisation of `<ticket>` and `<ip>` components
of the writer's filename template:

    INT-<ticket>-<ip>-<unix>-<6hex>.json

Allowed component chars per `intervention-writer` W1: `[A-Za-z0-9_-]+`.
Anything outside that set (path-traversal `..`, NUL, whitespace, unicode,
forward slash) MUST raise `InvalidFilenameComponent`.

The pure function `build_filename(payload, unix, hex_suffix)` is the
seam: tests pass a stable `unix` + `hex_suffix` so assertions stay
deterministic.
"""

from __future__ import annotations

import pytest

from nora.intervention_writer.filenames import (
    InvalidFilenameComponent,
    build_filename,
)

# ---------------------------------------------------------------------------
# W1 — happy-path filename
# ---------------------------------------------------------------------------


def test_build_filename_happy_path_ticket_and_ip() -> None:
    """Allowed `[A-Za-z0-9_-]+` chars on ticket + ip → expected filename."""
    payload = {"ticket_number": "TKT-001", "target_ip": "10.0.0.1"}
    name = build_filename(payload, unix=1700000000, hex_suffix="a1b2c3")
    assert name == "INT-TKT-001-10.0.0.1-1700000000-a1b2c3.json", (
        f"Happy-path filename mismatch; got: {name!r}"
    )


def test_build_filename_unix_is_ten_digit_epoch() -> None:
    """`<unix>` is interpolated as a 10-digit epoch."""
    payload = {"ticket_number": "TKT-001", "target_ip": "10.0.0.1"}
    name = build_filename(payload, unix=1700000000, hex_suffix="deadbe")
    # The 10-digit epoch is embedded verbatim between `-<ip>-` and `-<hex>.json`.
    assert "-1700000000-" in name, f"Expected 10-digit unix in filename; got: {name!r}"


def test_build_filename_hex_suffix_is_six_chars() -> None:
    """`<hex>` is the 6-char suffix provided by the caller (the writer's
    retry layer picks a fresh one on collision)."""
    payload = {"ticket_number": "TKT-001", "target_ip": "10.0.0.1"}
    name = build_filename(payload, unix=1700000000, hex_suffix="a1b2c3")
    # Stem before `.json` ends with the supplied hex suffix.
    assert name.endswith("-a1b2c3.json"), f"Expected -a1b2c3.json tail; got: {name!r}"


# ---------------------------------------------------------------------------
# W1 / W2 — path-traversal and invalid chars rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_ticket",
    [
        "OPS/PROD-12",  # forward slash
        "../etc/passwd",  # parent-dir traversal
        "TKT 001",  # whitespace
        "TKT\x001",  # NUL byte
        "TKT\u00e9001",  # unicode (é)
        "TKT..001",  # dot inside — `..` is a substring
        "TKT\t001",  # tab
        "TKT\n001",  # newline
    ],
)
def test_build_filename_rejects_invalid_ticket(bad_ticket: str) -> None:
    """A ticket with non `[A-Za-z0-9_-]` chars raises `InvalidFilenameComponent`."""
    payload = {"ticket_number": bad_ticket, "target_ip": "10.0.0.1"}
    with pytest.raises(InvalidFilenameComponent):
        build_filename(payload, unix=1700000000, hex_suffix="a1b2c3")


@pytest.mark.parametrize(
    "bad_ip",
    [
        "../../etc/passwd",  # parent-dir traversal
        "10.0.0.1/24",  # CIDR slash
        "10.0.0.1 ",  # trailing whitespace
        "10.0.0.1\u0000",  # NUL byte
        "10.0.0.\u00e9",  # unicode
    ],
)
def test_build_filename_rejects_invalid_ip(bad_ip: str) -> None:
    """An ip with non `[A-Za-z0-9_-]` chars raises `InvalidFilenameComponent`."""
    payload = {"ticket_number": "TKT-001", "target_ip": bad_ip}
    with pytest.raises(InvalidFilenameComponent):
        build_filename(payload, unix=1700000000, hex_suffix="a1b2c3")


def test_invalid_component_carries_offending_value() -> None:
    """`InvalidFilenameComponent` exposes the field name and offending string."""
    payload = {"ticket_number": "../etc", "target_ip": "10.0.0.1"}
    with pytest.raises(InvalidFilenameComponent) as exc:
        build_filename(payload, unix=1700000000, hex_suffix="a1b2c3")
    assert exc.value.field == "ticket_number", (
        f"InvalidFilenameComponent must name the field; got: {exc.value.field!r}"
    )
    assert exc.value.value == "../etc", (
        f"InvalidFilenameComponent must echo the offending value; got: {exc.value.value!r}"
    )


def test_build_filename_underscore_and_hyphen_accepted() -> None:
    """Underscore + hyphen + alnum are all allowed (the `[A-Za-z0-9_-]` set)."""
    payload = {"ticket_number": "TKT_001-ABC", "target_ip": "10-0-0-1"}
    name = build_filename(payload, unix=1700000000, hex_suffix="abcdef")
    assert name == "INT-TKT_001-ABC-10-0-0-1-1700000000-abcdef.json"
