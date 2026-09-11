"""R10 frozen redaction list — the contract is the test.

The redaction list is FROZEN by the spec (`specs/session-journal`). If you
change it here, you break the on-disk contract for every saved session.
"""

from __future__ import annotations

EXPECTED_FROZEN_LIST: frozenset[str] = frozenset(
    {
        "community",
        "community_string",
        "auth_password",
        "auth_key",
        "priv_password",
        "priv_key",
        "password",
        "ssh_password",
        "api_key",
        "token",
        "secret",
    }
)


def test_frozen_list_is_a_frozenset_of_strings() -> None:
    """`REDACTION_LIST` MUST be a `frozenset[str]` with exactly the 11 R10 keys."""
    from nora.core.session_redaction import REDACTION_LIST

    assert isinstance(REDACTION_LIST, frozenset), (
        f"REDACTION_LIST must be a frozenset; got {type(REDACTION_LIST).__name__}"
    )
    assert all(isinstance(k, str) for k in REDACTION_LIST), (
        f"All REDACTION_LIST members must be str; got {[type(k).__name__ for k in REDACTION_LIST]}"
    )
    assert REDACTION_LIST == EXPECTED_FROZEN_LIST, (
        f"REDACTION_LIST has drifted from R10.\n"
        f"  expected: {sorted(EXPECTED_FROZEN_LIST)}\n"
        f"  got:      {sorted(REDACTION_LIST)}\n"
        f"  missing:  {sorted(EXPECTED_FROZEN_LIST - REDACTION_LIST)}\n"
        f"  extra:    {sorted(REDACTION_LIST - EXPECTED_FROZEN_LIST)}"
    )


def test_frozen_list_size_is_eleven() -> None:
    """The frozen list MUST contain exactly 11 keys (per R10)."""
    from nora.core.session_redaction import REDACTION_LIST

    assert len(REDACTION_LIST) == 11, (
        f"R10 requires 11 keys; got {len(REDACTION_LIST)}: {sorted(REDACTION_LIST)}"
    )


def test_frozen_list_is_immutable() -> None:
    """`REDACTION_LIST` MUST be immutable (frozenset)."""
    from nora.core.session_redaction import REDACTION_LIST

    try:
        REDACTION_LIST.add("nope")  # type: ignore[attr-defined]
    except AttributeError:
        return  # expected — frozenset has no .add
    raise AssertionError("REDACTION_LIST is mutable; expected frozenset")


# ---------------------------------------------------------------------------
# `redact()` walker — pure recursive function over JSON-shaped values.
# ---------------------------------------------------------------------------


def test_redact_top_level_community_string_replaced() -> None:
    """R10-S1 — `kwargs["community"]` MUST be replaced with the marker."""
    from nora.core.session_redaction import _REDACTION_MARKER, redact

    out = redact({"device_id": "ap-7400-01", "community": "private"})
    assert out == {"device_id": "ap-7400-01", "community": _REDACTION_MARKER}


def test_redact_nested_api_key_replaced_siblings_pass_through() -> None:
    """R10-S2 — nested `api_key` redacted; `region` (not on the list) survives."""
    from nora.core.session_redaction import _REDACTION_MARKER, redact

    out = redact({"creds": {"api_key": "sk-test-1234", "region": "us-east"}})
    assert out == {"creds": {"api_key": _REDACTION_MARKER, "region": "us-east"}}


def test_redact_replaces_multiple_keys_at_depth() -> None:
    """Every key on the list MUST be redacted, regardless of depth or repetition."""
    from nora.core.session_redaction import _REDACTION_MARKER, redact

    payload = {
        "password": "alpha",
        "creds": {"api_key": "beta", "region": "us-east", "token": "gamma"},
        "items": [
            {"secret": "delta"},
            {"safe": "value"},
        ],
    }
    out = redact(payload)
    assert out["password"] == _REDACTION_MARKER
    assert out["creds"]["api_key"] == _REDACTION_MARKER
    assert out["creds"]["region"] == "us-east"
    assert out["creds"]["token"] == _REDACTION_MARKER
    assert out["items"][0]["secret"] == _REDACTION_MARKER
    assert out["items"][1]["safe"] == "value"


def test_redact_passes_through_non_string_keys() -> None:
    """A non-string key MUST NOT trigger redaction (only R10 names count)."""
    from nora.core.session_redaction import redact

    out = redact({0: "value", "community": "private"})
    assert out[0] == "value"
    assert out["community"] == "[REDACTED]"


def test_redact_returns_new_structure_does_not_mutate() -> None:
    """`redact()` MUST be pure — the input dict is never mutated."""
    from nora.core.session_redaction import redact

    original = {"community": "private", "device_id": "ap-7400-01"}
    snapshot = dict(original)
    redact(original)
    assert original == snapshot, f"redact() mutated input: {original!r}"


def test_redact_runs_before_sanitization_marker_survives() -> None:
    """R6 ordering — `_REDACTION_MARKER` survives the journal's sanitize pass.

    `Sanitizer` treats `REDACTED` as a serial and would replace it with
    `SERIAL_X`. The journal's `_sanitize_tree` MUST skip sanitization at
    keys on `REDACTION_LIST` so the marker survives. Sibling free-text
    values ARE sanitized normally.
    """
    from nora.core.session_journal import _sanitize_tree
    from nora.core.session_redaction import _REDACTION_MARKER, REDACTION_LIST, redact
    from nora.sanitizer import Sanitizer

    payload = {
        "community": "private",
        "device_id": "ap-7400-01",
        "note": "investigation at 10.0.0.5",
    }
    redacted = redact(payload)
    sanitized = _sanitize_tree(redacted, Sanitizer())

    # Marker survives (R10 ordering — redaction before sanitize).
    assert sanitized["community"] == _REDACTION_MARKER
    # Free-text sibling is sanitized (R6).
    assert "10.0.0.5" not in sanitized["note"], (
        f"Free-text sibling should have been sanitized; got: {sanitized['note']!r}"
    )
    # Sanity: REDACTION_LIST still covers `community`.
    assert "community" in REDACTION_LIST
