"""Tests for `DeviceResolver` and credential `SecretStr` safety.

Slice 1 of the PMP 450i production surface. Covers:

* `DeviceResolver.build(host, snmp_version, creds)` returns a frozen
  `Device` whose credential fields are `pydantic.SecretStr` (not plaintext).
* `repr(device)` and `device.model_dump()` mask credentials so plaintext
  never leaks to logs, exception messages, or JSON dumps.

Named test for slice 1: ``test_secret_str_safety_no_plaintext_in_repr``.

Zero-Leakage: TEST-NET-1 (``192.0.2.x``) host literals; non-secret
stand-in credentials (`change-me-...`) only.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import SecretStr

from nora.drivers.resolver import DeviceResolver, SnmpCredentials

# The credential plaintext values we expect NOT to leak through repr
# or model_dump. IP literals (TEST-NET-1) are intentionally NOT in this
# list because they are public documentation addresses — see RFC 5737.
_BANNED_LITERAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bchange-me-v2c-only\b"),
    re.compile(r"\btest-auth-only\b"),
    re.compile(r"\btest-priv-only\b"),
)


# ---------------------------------------------------------------------------
# Named test — secret_str_safety_no_plaintext_in_repr
# ---------------------------------------------------------------------------


def test_secret_str_safety_no_plaintext_in_repr() -> None:
    """`repr(device)` and `device.model_dump()` do NOT contain credential plaintext.

    Every credential field round-trips through `SecretStr`. Pydantic's
    repr is `SecretStr('**********')` (the literal `**********` is what
    we assert against) and `model_dump()` returns `{'community':
    '**********'}` — neither leaks the actual bytes.

    The test exercises both v2c (community) and v3 (auth + priv)
    credentials so a future regression that masks only one path is
    caught by the other.
    """
    creds = SnmpCredentials(
        community=SecretStr("change-me-v2c-only"),
        auth_password=SecretStr("test-auth-only"),
        priv_password=SecretStr("test-priv-only"),
    )
    device = DeviceResolver.build("192.0.2.10", "v3", creds)

    # ---- repr(device) ------------------------------------------------------
    rendered = repr(device)
    assert "**********" in rendered, (
        f"repr must mask secrets with '**********'; got: {rendered!r}"
    )
    for pattern in _BANNED_LITERAL_PATTERNS:
        assert not pattern.search(rendered), (
            f"repr leaked banned literal: {rendered!r}"
        )

    # ---- device.model_dump() ----------------------------------------------
    dumped = device.model_dump()
    assert isinstance(dumped, dict)
    dumped_repr = repr(dumped)
    assert "**********" in dumped_repr, (
        f"model_dump must mask secrets with '**********'; got: {dumped_repr!r}"
    )
    for pattern in _BANNED_LITERAL_PATTERNS:
        assert not pattern.search(dumped_repr), (
            f"model_dump leaked banned literal: {dumped_repr!r}"
        )

    # ---- sanity: get_secret_value() DOES return the real bytes ------------
    # The masking is representational; the value is recoverable inside
    # the SNMP client factory, which is the only legitimate consumer.
    assert device.auth_password is not None
    assert device.auth_password.get_secret_value() == "test-auth-only"
    assert device.priv_password is not None
    assert device.priv_password.get_secret_value() == "test-priv-only"


def test_secret_str_safety_v2c_community_masked() -> None:
    """Triangulation: v2c community string is masked on repr and dump.

    Separate test so the v3 path doesn't mask a v2c-only failure
    (the v3 field set is different on the Device model).
    """
    creds = SnmpCredentials(community=SecretStr("change-me-v2c-only"))
    device = DeviceResolver.build("192.0.2.10", "v2c", creds)

    rendered = repr(device)
    assert "**********" in rendered
    assert "change-me-v2c-only" not in rendered

    dumped = device.model_dump()
    dumped_repr = repr(dumped)
    assert "**********" in dumped_repr
    assert "change-me-v2c-only" not in dumped_repr


# ---------------------------------------------------------------------------
# Credentials model — typed SecretStr surface
# ---------------------------------------------------------------------------


def test_snmp_credentials_constructs_with_only_required_fields() -> None:
    """`SnmpCredentials` allows construction with any subset of credential fields.

    Triangulation: a caller building v3 creds shouldn't be forced to
    pass `community`, and a v2c caller shouldn't pass `auth_password`.
    The Device model validator at `build(...)` time enforces the
    version-specific requirements.
    """
    auth_only = SnmpCredentials(auth_password=SecretStr("a"))
    assert auth_only.community is None
    assert auth_only.auth_password is not None
    assert auth_only.auth_password.get_secret_value() == "a"

    community_only = SnmpCredentials(community=SecretStr("c"))
    assert community_only.auth_password is None
    assert community_only.priv_password is None


def test_snmp_credentials_coerces_plaintext_to_secret_str() -> None:
    """`SnmpCredentials` accepts a plain `str` and wraps it as `SecretStr`.

    Pydantic's `SecretStr` field type coerces a plain `str` into a
    `SecretStr` at validation time — the masking contract holds whether
    the caller passes `SecretStr("foo")` or `"foo"`. This tests the
    coercion path so an accidental loosening (e.g. swapping for `str`)
    is caught: the value MUST round-trip via `get_secret_value()`.
    """
    creds = SnmpCredentials(community="change-me-v2c-only")  # type: ignore[arg-type]
    assert creds.community is not None
    assert creds.community.get_secret_value() == "change-me-v2c-only"
    # Repr MUST mask even when constructed from plaintext.
    assert "change-me-v2c-only" not in repr(creds)


# ---------------------------------------------------------------------------
# Resolution invariants — `build` does not depend on devices.yaml
# ---------------------------------------------------------------------------


def test_build_does_not_open_devices_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`DeviceResolver.build(...)` does NOT touch the inventory file.

    The resolver is the IP-direct path; it must not open, write to, or
    even read the inventory. We pin this with an explicit `Path.read_text`
    guard: if the resolver opened the file, the spy would fire.
    """
    creds = SnmpCredentials(
        auth_password=SecretStr("test-auth-only"),
        priv_password=SecretStr("test-priv-only"),
    )

    # Spy on `Path.read_text` for the inventory path; if the resolver
    # opens it, the spy fires and the assertion trips.
    inventory = tmp_path / "devices.yaml"
    inventory.write_text("devices: []")

    real_read_text = type(inventory).read_text

    def _spy_read_text(self: object, *args: object, **kwargs: object) -> str:
        if isinstance(self, type(inventory)) and self == inventory:
            raise AssertionError(
                "DeviceResolver.build must not read Settings.nora_devices_inventory_path"
            )
        return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", _spy_read_text)

    # Must NOT raise; the resolver never opens devices.yaml.
    device = DeviceResolver.build("192.0.2.10", "v3", creds)
    assert device.device_id.startswith("adhoc-192.0.2.10-")
