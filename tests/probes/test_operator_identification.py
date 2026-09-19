"""Tests for `nora.probes.operator_identification.resolve_operator` (issue #61 / PR3 WU-3.14).

Resolution order:

1. ``settings.nora_operator_alias`` when set (non-empty).
2. ``header["X-NORA-Operator"]`` when set (case-insensitive).
3. The literal string ``"nora-operator"``.

The resolved alias is Sanitizer-cleaned — a private IPv4 literal in
the alias is masked before the function returns.
"""

from __future__ import annotations

from nora.probes.operator_identification import resolve_operator
from nora.sanitizer import Sanitizer


class _SettingsWithAlias:
    """Minimal settings stand-in supporting ``nora_operator_alias``."""

    def __init__(self, alias: str) -> None:
        self.nora_operator_alias = alias


class _SettingsEmptyAlias:
    """Settings with ``nora_operator_alias = ""`` — falls through to header / default."""

    nora_operator_alias = ""


class _SettingsNoAlias:
    """Settings with no ``nora_operator_alias`` attribute at all."""

    pass


def test_resolve_operator_prefers_settings_alias() -> None:
    """``settings.nora_operator_alias`` wins when set and non-empty."""
    settings = _SettingsWithAlias("ops-team")
    assert resolve_operator(settings=settings) == "ops-team"


def test_resolve_operator_falls_back_to_header() -> None:
    """When the settings alias is empty, the X-NORA-Operator header wins."""
    settings = _SettingsEmptyAlias()
    out = resolve_operator(settings=settings, header={"X-NORA-Operator": "alex"})
    assert out == "alex"


def test_resolve_operator_header_is_case_insensitive() -> None:
    """The header lookup is case-insensitive (RFC 7230 §3.2)."""
    settings = _SettingsEmptyAlias()
    out = resolve_operator(settings=settings, header={"x-nora-operator": "alex"})
    assert out == "alex"


def test_resolve_operator_falls_back_to_default() -> None:
    """When neither settings nor header is set, the default alias is returned."""
    settings = _SettingsNoAlias()
    out = resolve_operator(settings=settings)
    assert out == "nora-operator"


def test_resolve_operator_sanitizes_private_ip_in_alias() -> None:
    """A private IPv4 literal in the alias is masked before return."""
    settings = _SettingsWithAlias("ops 10.0.0.5")
    out = resolve_operator(settings=settings)
    assert "10.0.0.5" not in out, f"private IPv4 literal leaked through resolver; got {out!r}"


def test_resolve_operator_with_explicit_sanitizer() -> None:
    """The caller can pass a session-wide Sanitizer for stable aliases."""
    settings = _SettingsWithAlias("ops-team")
    san = Sanitizer()
    out1 = resolve_operator(settings=settings, sanitizer=san)
    out2 = resolve_operator(settings=settings, sanitizer=san)
    assert out1 == out2 == "ops-team"


def test_resolve_operator_empty_header_falls_through() -> None:
    """An empty-string header value is treated as absent (fall-through)."""
    settings = _SettingsNoAlias()
    out = resolve_operator(settings=settings, header={"X-NORA-Operator": "   "})
    assert out == "nora-operator"


def test_resolve_operator_settings_alias_whitespace_stripped() -> None:
    """Leading / trailing whitespace around the settings alias is stripped."""
    settings = _SettingsWithAlias("   ops-team   ")
    out = resolve_operator(settings=settings)
    assert out == "ops-team"
