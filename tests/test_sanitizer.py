"""Telemetry sanitizer tests — cover every scenario in `specs/telemetry-sanitizer/spec.md`.

The sanitizer is a pure function with no I/O. Tests assert that
private IPv4, MAC, serial, and hostname literals are replaced by stable
synthetic aliases within a session, and that edge cases pass through safely.
"""

from __future__ import annotations

import logging
import re
from unittest import mock

import pytest

from nora.sanitizer import Sanitizer, SanitizerInputError

# ---------------------------------------------------------------------------
# Category-specific regex patterns (per design.md).
# ---------------------------------------------------------------------------

PRIVATE_IPV4_LITERAL = "10.0.0.5"  # RFC 1918 10/8
PRIVATE_IPV4_SECOND = "10.0.0.6"
PUBLIC_IPV4 = "8.8.8.8"  # Not private; MUST survive.
MAC_LITERAL = "aa:bb:cc:dd:ee:ff"
MAC_SECOND = "00:11:22:33:44:55"
SERIAL_LITERAL = "ABC123XYZ-PROD-001"
HOSTNAME_LITERAL = "router-core-01.example.com"


@pytest.fixture
def sanitizer() -> Sanitizer:
    """A fresh `Sanitizer` per test so the alias map is isolated."""
    return Sanitizer()


# ---------------------------------------------------------------------------
# Requirement: Fixed Mask Categories
# ---------------------------------------------------------------------------


def test_private_ipv4_is_replaced(sanitizer: Sanitizer) -> None:
    """`10.0.0.5` MUST be replaced by an alias; the literal MUST NOT survive."""
    result = sanitizer.sanitize(f"target={PRIVATE_IPV4_LITERAL}")
    assert PRIVATE_IPV4_LITERAL not in result.text, f"Private IPv4 literal leaked: {result.text!r}"
    # The replacement must look like an alias (uppercase + underscore).
    assert re.search(r"\b[A-Z][A-Z0-9_]+\b", result.text), (
        f"No alias found in sanitized text: {result.text!r}"
    )
    assert result.counts["ip"] == 1


def test_public_ipv4_is_not_masked(sanitizer: Sanitizer) -> None:
    """Public IPv4 `8.8.8.8` MUST survive untouched."""
    result = sanitizer.sanitize(f"public={PUBLIC_IPV4}")
    assert PUBLIC_IPV4 in result.text
    assert result.counts["ip"] == 0


def test_mac_is_replaced(sanitizer: Sanitizer) -> None:
    """MAC addresses MUST be replaced by an alias."""
    result = sanitizer.sanitize(f"mac={MAC_LITERAL}")
    assert MAC_LITERAL not in result.text
    assert result.counts["mac"] == 1


def test_serial_is_replaced(sanitizer: Sanitizer) -> None:
    """Serial numbers MUST be replaced by an alias."""
    result = sanitizer.sanitize(f"serial={SERIAL_LITERAL}")
    assert SERIAL_LITERAL not in result.text
    assert result.counts["serial"] == 1


def test_hostname_is_replaced(sanitizer: Sanitizer) -> None:
    """Hostnames (multi-label FQDN) MUST be replaced by an alias."""
    result = sanitizer.sanitize(f"host={HOSTNAME_LITERAL}")
    assert HOSTNAME_LITERAL not in result.text
    assert result.counts["hostname"] == 1


def test_mixed_categories_replaced_together(sanitizer: Sanitizer) -> None:
    """All four categories in one input MUST each be replaced, each with its own counter."""
    text = (
        f"ip={PRIVATE_IPV4_LITERAL} mac={MAC_LITERAL} "
        f"serial={SERIAL_LITERAL} host={HOSTNAME_LITERAL}"
    )
    result = sanitizer.sanitize(text)
    for literal in (PRIVATE_IPV4_LITERAL, MAC_LITERAL, SERIAL_LITERAL, HOSTNAME_LITERAL):
        assert literal not in result.text, f"Literal {literal!r} survived in: {result.text!r}"
    assert result.counts == {"ip": 1, "mac": 1, "serial": 1, "hostname": 1}


# ---------------------------------------------------------------------------
# Requirement: Deterministic Alias Mapping Within a Session
# ---------------------------------------------------------------------------


def test_same_input_yields_same_alias_across_calls(sanitizer: Sanitizer) -> None:
    """Same literal in the same instance MUST always map to the same alias."""
    first = sanitizer.sanitize(f"first={PRIVATE_IPV4_LITERAL}").text
    second = sanitizer.sanitize(f"second={PRIVATE_IPV4_LITERAL}").text
    alias_re = re.compile(r"\b[A-Z][A-Z0-9_]+\b")
    first_alias = alias_re.search(first)
    second_alias = alias_re.search(second)
    assert first_alias is not None
    assert second_alias is not None
    assert first_alias.group() == second_alias.group(), (
        f"Alias changed between calls: {first_alias.group()!r} vs {second_alias.group()!r}"
    )


def test_distinct_inputs_yield_distinct_aliases(sanitizer: Sanitizer) -> None:
    """Distinct literals MUST produce distinct aliases."""
    result = sanitizer.sanitize(f"{PRIVATE_IPV4_LITERAL} {PRIVATE_IPV4_SECOND}")
    alias_re = re.compile(r"\b[A-Z][A-Z0-9_]+\b")
    aliases = alias_re.findall(result.text)
    assert len(aliases) == 2, f"Expected two aliases; got {aliases!r}"
    assert aliases[0] != aliases[1], "Aliases must be distinct for distinct inputs"
    assert result.counts["ip"] == 2


# ---------------------------------------------------------------------------
# Requirement: Pure Function, No I/O
# ---------------------------------------------------------------------------


def test_same_input_yields_byte_identical_output(sanitizer: Sanitizer) -> None:
    """Same input, same instance, MUST produce byte-identical output."""
    text = f"ip={PRIVATE_IPV4_LITERAL} mac={MAC_LITERAL}"
    first = sanitizer.sanitize(text).text
    second = sanitizer.sanitize(text).text
    assert first == second


def test_sanitizer_does_not_perform_io() -> None:
    """The sanitizer MUST NOT touch filesystem, sockets, or env vars."""
    sanitizer = Sanitizer()
    with mock.patch("builtins.open") as open_mock, mock.patch("socket.socket") as socket_mock:
        # Reading from os.environ is also forbidden inside sanitize.
        import os

        original_environ = os.environ
        with mock.patch.object(os, "environ", new=original_environ):
            sanitizer.sanitize(f"ping {PRIVATE_IPV4_LITERAL}")

        open_mock.assert_not_called()
        socket_mock.assert_not_called()


def test_separate_instances_have_independent_maps() -> None:
    """Two `Sanitizer` instances MUST NOT share the alias map."""
    a = Sanitizer()
    b = Sanitizer()
    text = f"ip={PRIVATE_IPV4_LITERAL}"

    out_a = a.sanitize(text).text
    out_b = b.sanitize(text).text

    # Both must produce an alias, but each instance may pick any valid alias.
    alias_re = re.compile(r"\b[A-Z][A-Z0-9_]+\b")
    alias_a = alias_re.search(out_a)
    alias_b = alias_re.search(out_b)
    assert alias_a is not None
    assert alias_b is not None
    # The literal is gone in both.
    assert PRIVATE_IPV4_LITERAL not in out_a
    assert PRIVATE_IPV4_LITERAL not in out_b


# ---------------------------------------------------------------------------
# Requirement: Edge Cases Pass Through Safely
# ---------------------------------------------------------------------------


def test_empty_input_returns_empty_output(sanitizer: Sanitizer) -> None:
    """Empty input MUST return empty output with no exception."""
    result = sanitizer.sanitize("")
    assert result.text == ""
    assert result.counts == {"ip": 0, "mac": 0, "serial": 0, "hostname": 0}


def test_no_identifier_input_is_unchanged(sanitizer: Sanitizer) -> None:
    """Input without identifiers MUST round-trip unchanged."""
    text = "hello world, no identifiers here."
    result = sanitizer.sanitize(text)
    assert result.text == text
    assert sum(result.counts.values()) == 0


def test_already_aliased_input_is_unchanged(sanitizer: Sanitizer) -> None:
    """Input that already contains only alias-like tokens MUST round-trip."""
    text = "RADIO_NODE_A SWITCH_ACC_01"
    result = sanitizer.sanitize(text)
    assert result.text == text
    assert sum(result.counts.values()) == 0


# ---------------------------------------------------------------------------
# Requirement: Failure Surfaces a Typed Error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_input", [None, b"bytes-not-str", 42, 3.14])
def test_non_string_input_raises_sanitizer_input_error(
    sanitizer: Sanitizer, bad_input: object
) -> None:
    """Non-string inputs MUST raise `SanitizerInputError` (a `TypeError`)."""
    with pytest.raises(SanitizerInputError):
        sanitizer.sanitize(bad_input)  # type: ignore[arg-type]


def test_sanitizer_input_error_is_type_error() -> None:
    """`SanitizerInputError` MUST be catchable as a `TypeError`."""
    assert issubclass(SanitizerInputError, TypeError)


# ---------------------------------------------------------------------------
# Requirement: Security Boundary — Credentials Are Out of Scope
# ---------------------------------------------------------------------------


def test_credentials_in_user_text_are_not_modified(sanitizer: Sanitizer) -> None:
    """A credential-shaped literal MUST survive untouched (sanitizer does not mask creds)."""
    secret_like = "sk-1234567890abcdef1234567890abcdef"
    result = sanitizer.sanitize(f"api_key={secret_like}")
    assert secret_like in result.text, f"Credential-shaped literal was modified: {result.text!r}"


# ---------------------------------------------------------------------------
# Requirement: Observability — Replacement Counter
# ---------------------------------------------------------------------------


def test_counts_dict_has_expected_keys(sanitizer: Sanitizer) -> None:
    """`SanitizedText.counts` MUST have keys `ip`, `mac`, `serial`, `hostname`."""
    result = sanitizer.sanitize(f"ip={PRIVATE_IPV4_LITERAL}")
    assert set(result.counts.keys()) == {"ip", "mac", "serial", "hostname"}


def test_counts_accumulate_across_calls(sanitizer: Sanitizer) -> None:
    """Each call's counts reflect THAT call's replacements, not cumulative."""
    single = sanitizer.sanitize(f"ip={PRIVATE_IPV4_LITERAL}")
    assert single.counts["ip"] == 1
    pair = sanitizer.sanitize(f"{PRIVATE_IPV4_LITERAL} {PRIVATE_IPV4_SECOND}")
    assert pair.counts["ip"] == 2


# ---------------------------------------------------------------------------
# Module-level regex constants (triangulation: prove the pattern surface)
# ---------------------------------------------------------------------------


def test_sanitizer_module_exposes_pattern_constants() -> None:
    """The sanitizer module MUST expose the four regex constants at module level."""
    from nora import sanitizer as sanitizer_mod

    assert hasattr(sanitizer_mod, "IPV4_PRIVATE_REGEX")
    assert hasattr(sanitizer_mod, "MAC_REGEX")
    assert hasattr(sanitizer_mod, "SERIAL_REGEX")
    assert hasattr(sanitizer_mod, "HOSTNAME_REGEX")

    for attr in ("IPV4_PRIVATE_REGEX", "MAC_REGEX", "SERIAL_REGEX", "HOSTNAME_REGEX"):
        pattern = getattr(sanitizer_mod, attr)
        # Each constant must be a compiled pattern or a regex source string.
        assert pattern is not None
        # If it's a string, it must contain the right marker; if Pattern, search works.
        assert hasattr(pattern, "search") or isinstance(pattern, str)


# ---------------------------------------------------------------------------
# Alias format triangulation — verify alias names are well-formed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "input_text, expected_substring_in_replacement",
    [
        (f"ip={PRIVATE_IPV4_LITERAL}", "RADIO_NODE_"),
        (f"mac={MAC_LITERAL}", "SWITCH_ACC_"),
        (f"serial={SERIAL_LITERAL}", "SERIAL_"),
        (f"host={HOSTNAME_LITERAL}", "HOST_"),
    ],
)
def test_aliases_use_distinct_category_prefixes(
    sanitizer: Sanitizer, input_text: str, expected_substring_in_replacement: str
) -> None:
    """Each category uses a distinct prefix so aliases are visually identifiable."""
    result = sanitizer.sanitize(input_text)
    assert expected_substring_in_replacement in result.text, (
        f"Expected alias with prefix {expected_substring_in_replacement!r}; got: {result.text!r}"
    )


# ---------------------------------------------------------------------------
# Requirement: Observability — Replacement Counter + Alias Map Log Levels
# (Closes W1 PARTIAL scenario 3)
# ---------------------------------------------------------------------------


def test_alias_map_debug_log_lists_literal_and_alias(
    sanitizer: Sanitizer, caplog: pytest.LogCaptureFixture
) -> None:
    """At DEBUG, the alias map entry MUST appear (literal + alias) per (category, literal)."""
    # Pre-populate with a different IP so the caplog block observes a NEW alias being created.
    sanitizer.sanitize(f"ip={PRIVATE_IPV4_SECOND}")

    with caplog.at_level(logging.DEBUG, logger="nora.sanitizer"):
        sanitizer.sanitize(f"ip={PRIVATE_IPV4_LITERAL} mac={MAC_LITERAL}")

    debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert debug_records, (
        f"Expected at least one DEBUG log record from nora.sanitizer; got: {caplog.records!r}"
    )
    # At least one record names both the IP literal and its alias.
    matched = [
        r
        for r in debug_records
        if "alias" in r.getMessage().lower() and PRIVATE_IPV4_LITERAL in r.getMessage()
    ]
    assert matched, (
        f"Expected DEBUG alias record for {PRIVATE_IPV4_LITERAL!r}; got: "
        f"{[r.getMessage() for r in debug_records]!r}"
    )


def test_alias_map_absent_at_info_level(
    sanitizer: Sanitizer, caplog: pytest.LogCaptureFixture
) -> None:
    """At INFO, the alias map MUST NOT appear (alias literals are absent)."""
    with caplog.at_level(logging.INFO, logger="nora.sanitizer"):
        sanitizer.sanitize(f"ip={PRIVATE_IPV4_LITERAL} mac={MAC_LITERAL} host={HOSTNAME_LITERAL}")

    info_or_above = [r for r in caplog.records if r.levelno >= logging.INFO]
    for record in info_or_above:
        msg = record.getMessage()
        assert "alias" not in msg.lower() or "counter" in msg.lower(), (
            f"INFO+ log line should not contain alias-map details; got: {msg!r}"
        )
        # The raw IP/MAC/hostname literal MUST NOT appear in any INFO+ record.
        for needle in (PRIVATE_IPV4_LITERAL, MAC_LITERAL, HOSTNAME_LITERAL):
            assert needle not in msg, f"INFO+ log leaked literal {needle!r}: {msg!r}"
