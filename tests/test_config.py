"""Configuration tests — cover every scenario in `specs/secure-configuration/spec.md`.

The tests verify the trimmed `Settings` shape (7 user-settable fields
after the thin split), the precedence swap, the `.env.example` contract,
and the `.env` gitignore rule.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"
GITIGNORE = PROJECT_ROOT / ".gitignore"

# Regex that catches real private IPv4 literals. `10.0.0.5`, `172.16.0.1`, etc.
PRIVATE_IPV4 = re.compile(
    r"\b(?:10\.\d+\.\d+\.\d+|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+|192\.168\.\d+\.\d+|127\.\d+\.\d+\.\d+)\b"
)


# --- Requirement: Pydantic Settings Is the Only Configuration Source --------


def test_settings_has_eight_user_fields() -> None:
    """After the thin split, `Settings.model_fields` has exactly 29 entries.

    Twenty-eight user-settable fields plus the computed `loaded_from` = 29 total.
    (Reconcile of main @ 4c6399a + ICMP @ 8b4dd3d + ICMP PR2 @ 57764f1:
    22 from main + 6 from PR1 + 1 from PR2 = 29.)
    The nine former LLM/journal fields MUST be gone.

    PR 4 (slice 4) extends the set with four HITL/maintenance-window
    fields: ``nora_maintenance_window_minutes``,
    ``nora_maintenance_window_start_minutes_ago``,
    ``nora_hitl_rollback_timeout_seconds``, ``nora_hitl_token_ttl_seconds``.

    Issue #43 (`2026-09-15-3tier-tool-governance`) extends the set with
    two more: ``nora_hitl_signing_key``, ``nora_tool_specs_dir``.

    WU-4 / PR #44 follow-up extends the set with one more:
    ``nora_hitl_admin_enabled`` (gate for the `hitl_mint_token` MCP admin
    tool, default True per user decision 2026-09-17).

    WU-A (feat/multi-community-band-reboot) extends the set with one more:
    ``nora_preflight_community_validation`` (pre-flight community
    validation gate, default True).

    WU-3 (issue #62) extends the set with three more:
    ``nora_spectrum_sweep_duration_seconds``,
    ``nora_spectrum_sweep_poll_interval_seconds``,
    ``nora_spectrum_sweep_timeout_seconds``.

    Issue #70 / WU-1 extends the set with five more:
    ``nora_spectrum_http_timeout_seconds``,
    ``nora_spectrum_http_max_retries``,
    ``nora_spectrum_http_retry_delay_seconds``,
    ``nora_spectrum_sm_reassociation_timeout_seconds``,
    ``nora_spectrum_ranking_top_n``.

    Issue #61 / ICMP probe extends the set with six more:
    ``nora_icmp_default_duration_seconds``, ``nora_icmp_default_interval_seconds``,
    ``nora_icmp_default_packet_size_bytes``, ``nora_icmp_per_packet_timeout_seconds``,
    ``nora_icmp_max_duration_seconds``, ``nora_icmp_min_duration_seconds``.
    Enforced by ``_validate_icmp_duration_bounds`` (min <= default <= max).

    Issue #61 PR2 (WU-2.6) extends the set with one more:
    ``nora_probe_results_dir`` (atomic JSON snapshots of completed
    probe runs; mirrors ``nora_interventions_dir``).
    """
    from nora.config import Settings

    fields = Settings.model_fields
<<<<<<< HEAD
    assert len(fields) == 29, (
        f"Expected 29 model fields (28 user + loaded_from); got {len(fields)}: {sorted(fields)}"
    )
    )

    forbidden = {
        "nora_llm_provider",
        "lmstudio_api_host",
        "lmstudio_model_id",
        "gemini_api_key",
        "gemini_model_id",
        "nora_session_journal_dir",
        "nora_session_trace_max_steps",
        "nora_session_journal_enabled",
        "nora_operator_alias",
    }
    leaked = forbidden & set(fields)
    assert not leaked, f"LLM/journal fields leaked into Settings: {sorted(leaked)}"


def test_pyproject_has_no_llm_sdk_deps() -> None:
    """`pyproject.toml` MUST NOT list `lmstudio` or `google-genai` after the thin split."""
    import tomllib

    data = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    deps = data["project"]["dependencies"]
    forbidden = {"lmstudio", "google-genai"}
    leaked = {d for d in deps if any(f in d for f in forbidden)}
    assert not leaked, f"LLM SDK deps leaked into pyproject.toml: {sorted(leaked)}"


def test_settings_loads_provider_from_env_file(tmp_path: Path) -> None:
    """`Settings()` reads `NORA_LLM_PROVIDER` from the configured `.env` file.

    Kept after the thin split as a smoke test for legacy .env files that
    still declare the LLM keys; Pydantic `extra='ignore'` swallows them
    and the test asserts the surviving fields still load.
    """
    env_file = tmp_path / ".env"
    env_file.write_text("NORA_LLM_PROVIDER=lmstudio\nLMSTUDIO_MODEL_ID=test-model\n")

    from nora.config import Settings

    settings = Settings(_env_file=str(env_file))

    # Legacy keys are silently ignored; surviving defaults stay.
    assert settings.nora_oid_catalogs_path == Path("./data/oid-catalogs/")


def test_no_os_environ_in_src_nora() -> None:
    """Code under `src/nora/` MUST NOT call `os.environ` directly.

    The only allow-listed modules are `config.py` (the Pydantic Settings
    boundary), `cli.py` (the entry point that sets process env before
    importing fastmcp) and `__main__.py` (the deprecation alias). Every
    other module must read settings through the Settings instance.
    """
    src = PROJECT_ROOT / "src" / "nora"
    allow_list = {"config.py", "__main__.py", "cli.py"}
    offenders: list[tuple[Path, int, str]] = []
    pattern = re.compile(r"\bos\.environ\b")
    for py in src.rglob("*.py"):
        if py.name in allow_list:
            continue
        for lineno, line in enumerate(py.read_text().splitlines(), start=1):
            if pattern.search(line):
                offenders.append((py, lineno, line.strip()))
    assert offenders == [], (
        "Direct os.environ access is forbidden outside the allow-list; "
        f"offenders: {[(str(p), n) for p, n, _ in offenders]}"
    )


def test_extra_unknown_settings_are_ignored(tmp_path: Path) -> None:
    """Unknown keys in `.env` are silently ignored."""
    from nora.config import Settings

    env_file = tmp_path / ".env"
    env_file.write_text("NORA_UNKNOWN_KEY=foo\nNORA_OID_CATALOG_SIGNING_KEY=change-me\n")

    settings = Settings(_env_file=str(env_file))
    # No exception means the unknown key was ignored; signing key was loaded.
    assert settings.nora_oid_catalog_signing_key is not None


def test_missing_required_setting_fails_fast() -> None:
    """An empty `nora_oid_catalog_signing_key` with no override is allowed at boot.

    After the thin split the only "required" contract is that the
    `OidCatalogRegistry.verify_all` boot step fails closed when the
    signing key is empty. Pydantic itself does NOT enforce a non-None
    signing key (operators can wire it via `.env`), but the boot
    verifier rejects an empty value. The `Settings` constructor must
    NOT raise when the field is absent.
    """
    from nora.config import Settings

    settings = Settings(
        _env_file=None,
        _env_file_encoding=None,
    )
    assert settings.nora_oid_catalog_signing_key is None


# --- Requirement: Synthetic `.env.example` ----------------------------------


def test_env_example_lists_every_read_variable() -> None:
    """`.env.example` mirrors every `Settings` field that the user can set.

    `loaded_from` is a computed field set by the validator; it is intentionally
    absent from the user-facing template.
    """
    from nora.config import Settings

    env_text = ENV_EXAMPLE.read_text()
    declared = set(re.findall(r"^([A-Z][A-Z0-9_]+)\s*=", env_text, re.MULTILINE))

    user_settable_fields = {name for name in Settings.model_fields if name != "loaded_from"}
    missing = {name.upper() for name in user_settable_fields} - declared
    assert not missing, f".env.example is missing keys for fields: {sorted(missing)}"


def test_env_example_contains_only_synthetic_placeholders() -> None:
    """No real credentials, IPs, MACs, hostnames, or serials in `.env.example`."""
    assert ENV_EXAMPLE.exists(), ".env.example must exist at the project root"

    text = ENV_EXAMPLE.read_text()
    offenders: list[str] = []

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Each non-comment line should look like KEY=VALUE or KEY="VALUE"
        if "=" not in line:
            continue
        _, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")

        # No real private IPv4.
        if PRIVATE_IPV4.search(value):
            offenders.append(f"private IPv4 literal: {line!r}")
        # No MAC address.
        if re.search(r"(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}", value):
            offenders.append(f"MAC literal: {line!r}")
        # No real-looking hostname (heuristic: FQDN with multiple labels, but no
        # synthetic placeholders like change-me / example.com are flagged).
        if re.search(r"\b[a-z0-9-]+\.[a-z0-9-]+\.[a-z]{2,}\b", value):
            # Allow RFC 5737 docs ranges, example.com, change-me etc.
            if not re.search(
                r"example\.com|change-?me|your-.*-here|localhost|placeholder", value, re.I
            ):
                offenders.append(f"hostname literal: {line!r}")

    assert offenders == [], f".env.example contains non-synthetic values: {offenders}"


# --- Requirement: Credentials Never Appear in String Representations --------


def test_repr_masks_secret_fields(tmp_path: Path) -> None:
    """`repr(Settings())` MUST NOT contain the literal signing key value."""
    from nora.config import Settings

    env_file = tmp_path / ".env"
    env_file.write_text("NORA_OID_CATALOG_SIGNING_KEY=do-not-leak-this-key\n")

    settings = Settings(_env_file=str(env_file))

    rendered = repr(settings)
    assert "do-not-leak-this-key" not in rendered, f"repr() leaked the API key: {rendered}"
    # The masked SecretStr form (e.g., '**********') is the expected output.
    assert "**********" in rendered or "SecretStr" in rendered, (
        f"Expected masked SecretStr in repr(); got: {rendered}"
    )


def test_log_lines_never_contain_secrets(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A log line containing the Settings object MUST NOT echo any secret."""
    from nora.config import Settings

    env_file = tmp_path / ".env"
    env_file.write_text("NORA_OID_CATALOG_SIGNING_KEY=hidden-secret-value\n")
    settings = Settings(_env_file=str(env_file))

    with caplog.at_level(logging.INFO):
        logging.getLogger("nora.test").info("settings=%r", settings)

    joined = "\n".join(caplog.messages)
    assert "hidden-secret-value" not in joined, f"Log line leaked the secret: {joined!r}"


# --- Requirement: Settings Load Status Is Observable ------------------------


def test_env_file_takes_precedence_over_process_env(tmp_path: Path, monkeypatch) -> None:
    """`.env` wins over process environment for matching keys.

    We use `NORA_OID_CATALOGS_PATH` (a surviving field) for the precedence
    check; we then separately assert `loaded_from == ".env"`.
    """
    from nora.config import Settings

    env_file = tmp_path / ".env"
    env_file.write_text("NORA_OID_CATALOGS_PATH=./data/from-dotenv/\n")

    # Process env exports a conflicting value for the same key.
    monkeypatch.setenv("NORA_OID_CATALOGS_PATH", "./data/from-process-env/")

    settings = Settings(_env_file=str(env_file))
    assert settings.nora_oid_catalogs_path == Path("./data/from-dotenv/")
    assert settings.loaded_from == ".env"


def test_process_env_is_used_when_no_env_file(monkeypatch) -> None:
    """When no `.env` is present, process env vars are loaded."""
    from nora.config import Settings

    monkeypatch.setenv("NORA_OID_CATALOGS_PATH", "/etc/nora/from-process-env/")

    settings = Settings(_env_file=None, _env_file_encoding=None)
    assert settings.nora_oid_catalogs_path == Path("/etc/nora/from-process-env/")
    assert settings.loaded_from == "process_env"


def test_defaults_are_explicit_when_nothing_is_set(monkeypatch, tmp_path: Path) -> None:
    """When no `.env` and no relevant env vars exist, defaults apply and loaded_from=='defaults'."""
    import os

    from nora.config import Settings

    # Strip every NORA_/LMSTUDIO_/GEMINI_ env var so the test is hermetic.
    for key in list(os.environ):
        if key.startswith(("NORA_", "LMSTUDIO_", "GEMINI_")):
            monkeypatch.delenv(key, raising=False)

    settings = Settings(_env_file=None, _env_file_encoding=None)

    assert settings.nora_oid_catalogs_path == Path("./data/oid-catalogs/")
    assert settings.nora_devices_inventory_path == Path("./data/devices.yaml")
    assert settings.nora_oid_catalog_signing_key is None
    assert settings.nora_prompts_dir is None
    assert settings.nora_interventions_dir == Path("./var/interventions/")
    assert settings.nora_interventions_keyword_search_max_records == 1000
    assert settings.nora_interventions_correlate_scan_limit == 50
    assert settings.loaded_from == "defaults"


# --- Requirement: `.env` Is Never Tracked -----------------------------------


def test_env_is_listed_in_gitignore() -> None:
    """`.env` MUST appear in `.gitignore` so it is excluded from the index."""
    gitignore_text = GITIGNORE.read_text()
    assert re.search(r"^\.env\b", gitignore_text, re.MULTILINE), (
        f".env is not gitignored. Current .gitignore:\n{gitignore_text}"
    )


# --- Requirement: Intervention-memory MCP Settings fields (Phase 3) ---------
#
# Three env-driven fields, defaults per spec R8:
#   nora_interventions_dir                       = ./var/interventions/
#   nora_interventions_keyword_search_max_records = 1000
#   nora_interventions_correlate_scan_limit      = 50


def test_intervention_memory_settings_have_safe_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """All three intervention-memory Settings fields exist with the documented defaults."""
    import os

    from nora.config import Settings

    # Hermetic env: strip every NORA_/LMSTUDIO_/GEMINI_ var so defaults apply.
    for key in list(os.environ):
        if key.startswith(("NORA_", "LMSTUDIO_", "GEMINI_")):
            monkeypatch.delenv(key, raising=False)

    settings = Settings(_env_file=None, _env_file_encoding=None)

    assert settings.nora_interventions_dir == Path("./var/interventions/"), (
        f"nora_interventions_dir default wrong: {settings.nora_interventions_dir!r}"
    )
    assert settings.nora_interventions_keyword_search_max_records == 1000, (
        f"keyword_search default wrong: {settings.nora_interventions_keyword_search_max_records!r}"
    )
    assert settings.nora_interventions_correlate_scan_limit == 50, (
        f"correlate_scan default wrong: {settings.nora_interventions_correlate_scan_limit!r}"
    )


def test_intervention_memory_settings_override_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each intervention-memory Settings field is overridable via env."""
    from nora.config import Settings

    monkeypatch.setenv("NORA_INTERVENTIONS_DIR", "/tmp/custom-records/")
    monkeypatch.setenv("NORA_INTERVENTIONS_KEYWORD_SEARCH_MAX_RECORDS", "500")
    monkeypatch.setenv("NORA_INTERVENTIONS_CORRELATE_SCAN_LIMIT", "25")

    settings = Settings(_env_file=None, _env_file_encoding=None)

    assert settings.nora_interventions_dir == Path("/tmp/custom-records/")
    assert settings.nora_interventions_keyword_search_max_records == 500
    assert settings.nora_interventions_correlate_scan_limit == 25


def test_env_example_documents_three_intervention_memory_keys() -> None:
    """`.env.example` MUST list every intervention-memory key with a sanitized placeholder.

    Per spec R8 — no real production paths. Defaults point at relative
    `./var/interventions/`. The two cap keys are uncommented overrides.
    """
    env_text = ENV_EXAMPLE.read_text()
    declared = set(re.findall(r"^([A-Z][A-Z0-9_]+)\s*=", env_text, re.MULTILINE))

    expected_keys = {
        "NORA_INTERVENTIONS_DIR",
        "NORA_INTERVENTIONS_KEYWORD_SEARCH_MAX_RECORDS",
        "NORA_INTERVENTIONS_CORRELATE_SCAN_LIMIT",
    }
    missing = expected_keys - declared
    assert not missing, f".env.example missing intervention-memory keys: {sorted(missing)}"

    # No real credentials, IPs, MACs, hostnames in the new section.
    offenders: list[str] = []
    for line in env_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() not in expected_keys:
            continue
        v = value.strip().strip('"').strip("'")
        if PRIVATE_IPV4.search(v):
            offenders.append(f"{key}: private IPv4 literal {v!r}")
        if re.search(r"(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}", v):
            offenders.append(f"{key}: MAC literal {v!r}")
        if re.search(r"\b[a-z0-9-]+\.[a-z0-9-]+\.[a-z]{2,}\b", v):
            if not re.search(r"example\.com|change-?me|localhost|placeholder", v, re.I):
                offenders.append(f"{key}: hostname literal {v!r}")
    assert offenders == [], f".env.example has non-synthetic values: {offenders}"


# --- Requirement: HITL signing key + tool-spec dir (issue #43) -------------


def test_settings_has_new_hitl_signing_key_field() -> None:
    """`Settings` exposes `nora_hitl_signing_key: SecretStr | None`.

    Per `secure-configuration` scenario "Settings exposes
    nora_hitl_signing_key as SecretStr". The field defaults to `None`
    (lazy fail-closed at Tier-2 invocation time).
    """
    import os

    from nora.config import Settings

    for key in list(os.environ):
        if key.startswith("NORA_"):
            os.environ.pop(key, None)

    settings = Settings(_env_file=None, _env_file_encoding=None)
    assert hasattr(settings, "nora_hitl_signing_key")
    assert settings.nora_hitl_signing_key is None, (
        "nora_hitl_signing_key default must be None (lazy fail-closed at Tier-2 invocation)"
    )


def test_settings_loads_hitl_signing_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """`NORA_HITL_SIGNING_KEY` env / `.env` is loaded into `Settings.nora_hitl_signing_key`.

    Per `secure-configuration` scenario "Settings exposes
    nora_hitl_signing_key as SecretStr". `repr()` MUST mask the value.
    """
    from pydantic import SecretStr

    from nora.config import Settings

    monkeypatch.setenv("NORA_HITL_SIGNING_KEY", "test-only-key-do-not-use")
    settings = Settings(_env_file=None, _env_file_encoding=None)

    assert isinstance(settings.nora_hitl_signing_key, SecretStr)
    assert settings.nora_hitl_signing_key.get_secret_value() == "test-only-key-do-not-use"
    assert "test-only-key-do-not-use" not in repr(settings), (
        f"repr() leaked the signing key: {repr(settings)!r}"
    )


def test_settings_has_tool_specs_dir_field_with_default() -> None:
    """`Settings.nora_tool_specs_dir` defaults to `Path(\"docs/tool_specs\")`.

    Per `secure-configuration` scenario "default tool-spec dir is
    resolved against the repo root".
    """
    import os

    from nora.config import Settings

    for key in list(os.environ):
        if key.startswith("NORA_"):
            os.environ.pop(key, None)

    settings = Settings(_env_file=None, _env_file_encoding=None)
    assert hasattr(settings, "nora_tool_specs_dir")
    # The default is `Path("docs/tool_specs")` — resolved relative to the
    # process CWD. The assertion only pins the field exists and is a Path.
    from pathlib import Path

    assert isinstance(settings.nora_tool_specs_dir, Path)
    assert settings.nora_tool_specs_dir.name == "tool_specs"


def test_settings_tool_specs_dir_overrides_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """`NORA_TOOL_SPECS_DIR` overrides the default tool-spec dir.

    Per `secure-configuration` scenario "operator override wins over default".
    """
    from pathlib import Path

    from nora.config import Settings

    monkeypatch.setenv("NORA_TOOL_SPECS_DIR", "/etc/nora/tool_specs/")
    settings = Settings(_env_file=None, _env_file_encoding=None)
    assert settings.nora_tool_specs_dir == Path("/etc/nora/tool_specs/")


def test_env_example_lists_two_new_hitl_and_tool_spec_keys() -> None:
    """`.env.example` lists `NORA_HITL_SIGNING_KEY` and `NORA_TOOL_SPECS_DIR`.

    Per `secure-configuration` scenario ".env.example lists every new field".
    Placeholder values only.
    """
    env_text = ENV_EXAMPLE.read_text()
    declared = set(re.findall(r"^([A-Z][A-Z0-9_]+)\s*=", env_text, re.MULTILINE))

    for key in ("NORA_HITL_SIGNING_KEY", "NORA_TOOL_SPECS_DIR"):
        assert key in declared, f".env.example missing {key!r}"
