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
