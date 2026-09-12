"""Configuration tests — cover every scenario in `specs/secure-configuration/spec.md`.

The tests are written first (RED). They MUST fail until `src/nora/config.py`
ships a `Settings` class with the documented behaviour.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"
GITIGNORE = PROJECT_ROOT / ".gitignore"

# Regex that catches real private IPv4 literals. `10.0.0.5`, `172.16.0.1`, etc.
PRIVATE_IPV4 = re.compile(
    r"\b(?:10\.\d+\.\d+\.\d+|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+|192\.168\.\d+\.\d+|127\.\d+\.\d+\.\d+)\b"
)


# --- Requirement: Pydantic Settings Is the Only Configuration Source --------


def test_settings_loads_provider_from_env_file(tmp_path: Path) -> None:
    """`Settings()` reads `NORA_LLM_PROVIDER` from the configured `.env` file."""
    env_file = tmp_path / ".env"
    env_file.write_text("NORA_LLM_PROVIDER=lmstudio\nLMSTUDIO_MODEL_ID=test-model\n")

    # Late import so a missing `nora.config` fails the test cleanly.
    from nora.config import Settings

    settings = Settings(_env_file=str(env_file))

    assert settings.nora_llm_provider == "lmstudio"
    assert settings.lmstudio_model_id == "test-model"


def test_no_os_environ_in_src_nora() -> None:
    """Code under `src/nora/` MUST NOT call `os.environ` directly.

    The only allow-listed modules are `config.py` (the Pydantic Settings
    boundary) and `__main__.py` (the entry point that needs to set process
    env before importing fastmcp). Every other module must read settings
    through the Settings instance.
    """
    src = PROJECT_ROOT / "src" / "nora"
    allow_list = {"config.py", "__main__.py"}
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
    env_file.write_text("NORA_UNKNOWN_KEY=foo\nNORA_LLM_PROVIDER=lmstudio\n")

    settings = Settings(_env_file=str(env_file))
    assert settings.nora_llm_provider == "lmstudio"


def test_missing_required_setting_fails_fast() -> None:
    """`NORA_LLM_PROVIDER=gemini` without `GEMINI_API_KEY` raises ValidationError."""
    from nora.config import Settings

    with pytest.raises(ValidationError) as excinfo:
        Settings(
            _env_file=None,
            nora_llm_provider="gemini",
        )
    message = str(excinfo.value)
    assert "GEMINI_API_KEY" in message or "gemini_api_key" in message, (
        f"Validation error should name GEMINI_API_KEY; got: {message}"
    )


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


# --- Requirement: Provider Credential Isolation -----------------------------


def test_lmstudio_active_does_not_require_gemini_api_key(tmp_path: Path) -> None:
    """When provider=lmstudio, no GEMINI_API_KEY is needed."""
    from nora.config import Settings

    env_file = tmp_path / ".env"
    env_file.write_text("NORA_LLM_PROVIDER=lmstudio\n")

    # No exception is the assertion.
    settings = Settings(_env_file=str(env_file))
    assert settings.nora_llm_provider == "lmstudio"
    assert settings.gemini_api_key is None


def test_gemini_active_requires_gemini_api_key(tmp_path: Path) -> None:
    """When provider=gemini and GEMINI_API_KEY is missing, startup aborts."""
    from nora.config import Settings

    env_file = tmp_path / ".env"
    env_file.write_text("NORA_LLM_PROVIDER=gemini\n")

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=str(env_file))
    assert "gemini_api_key" in str(excinfo.value).lower() or "GEMINI_API_KEY" in str(excinfo.value)


# --- Requirement: Credentials Never Appear in String Representations --------


def test_repr_masks_secret_fields(tmp_path: Path) -> None:
    """`repr(Settings())` MUST NOT contain the literal API key value."""
    from nora.config import Settings

    env_file = tmp_path / ".env"
    env_file.write_text("NORA_LLM_PROVIDER=gemini\nGEMINI_API_KEY=do-not-leak-this-key\n")

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
    env_file.write_text("NORA_LLM_PROVIDER=gemini\nGEMINI_API_KEY=hidden-secret-value\n")
    settings = Settings(_env_file=str(env_file))

    with caplog.at_level(logging.INFO):
        logging.getLogger("nora.test").info("settings=%r", settings)

    joined = "\n".join(caplog.messages)
    assert "hidden-secret-value" not in joined, f"Log line leaked the secret: {joined!r}"


# --- Requirement: Settings Load Status Is Observable ------------------------


def test_env_file_takes_precedence_over_process_env(tmp_path: Path, monkeypatch) -> None:
    """`.env` wins over process environment for matching keys.

    We use `LMSTUDIO_MODEL_ID` for the precedence check so the test does not
    trip the gemini-credential validator; we then separately assert
    `loaded_from == ".env"`.
    """
    from nora.config import Settings

    env_file = tmp_path / ".env"
    env_file.write_text("NORA_LLM_PROVIDER=lmstudio\nLMSTUDIO_MODEL_ID=from-dotenv\n")

    # Process env exports a conflicting value for the same key.
    monkeypatch.setenv("LMSTUDIO_MODEL_ID", "from-process-env")

    settings = Settings(_env_file=str(env_file))
    assert settings.lmstudio_model_id == "from-dotenv"
    assert settings.loaded_from == ".env"


def test_process_env_is_used_when_no_env_file(monkeypatch) -> None:
    """When no `.env` is present, process env vars are loaded."""
    from nora.config import Settings

    monkeypatch.setenv("NORA_LLM_PROVIDER", "lmstudio")
    monkeypatch.setenv("LMSTUDIO_MODEL_ID", "process-env-model")

    settings = Settings(_env_file=None, _env_file_encoding=None)
    assert settings.nora_llm_provider == "lmstudio"
    assert settings.lmstudio_model_id == "process-env-model"
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

    assert settings.nora_llm_provider == "lmstudio"
    assert settings.lmstudio_api_host == "localhost:1234"
    assert settings.lmstudio_model_id == "qwen2.5-7b-instruct"
    assert settings.gemini_api_key is None
    assert settings.gemini_model_id == "gemini-2.5-flash"
    assert settings.loaded_from == "defaults"


# --- Requirement: `.env` Is Never Tracked -----------------------------------


def test_env_is_listed_in_gitignore() -> None:
    """`.env` MUST appear in `.gitignore` so it is excluded from the index."""
    gitignore_text = GITIGNORE.read_text()
    assert re.search(r"^\.env\b", gitignore_text, re.MULTILINE), (
        f".env is not gitignored. Current .gitignore:\n{gitignore_text}"
    )


# --- Requirement: SessionJournal Settings Fields (Phase 2) -----------------
#
# Four new env-driven settings. Defaults are safe:
#   nora_session_journal_dir  = ./var/sessions/
#   nora_session_trace_max_steps = 50
#   nora_session_journal_enabled = true
#   nora_operator_alias = "anonymous"


def test_session_journal_settings_have_safe_defaults(tmp_path: Path, monkeypatch) -> None:
    """All four SessionJournal Settings fields exist with the documented defaults."""
    # Hermetic env: strip every NORA_/LMSTUDIO_/GEMINI_ var so the test is deterministic.
    import os

    from nora.config import Settings

    for key in list(os.environ):
        if key.startswith(("NORA_", "LMSTUDIO_", "GEMINI_")):
            monkeypatch.delenv(key, raising=False)

    settings = Settings(_env_file=None, _env_file_encoding=None)

    # Field existence + defaults (paths compared via string for portability).
    assert settings.nora_session_journal_dir == Path("./var/sessions/"), (
        f"nora_session_journal_dir default wrong: {settings.nora_session_journal_dir!r}"
    )
    assert settings.nora_session_trace_max_steps == 50, (
        f"nora_session_trace_max_steps default wrong: {settings.nora_session_trace_max_steps!r}"
    )
    assert settings.nora_session_journal_enabled is True, (
        f"nora_session_journal_enabled default wrong: {settings.nora_session_journal_enabled!r}"
    )
    assert settings.nora_operator_alias == "anonymous", (
        f"nora_operator_alias default wrong: {settings.nora_operator_alias!r}"
    )


def test_session_journal_settings_override_from_env(tmp_path: Path, monkeypatch) -> None:
    """Each SessionJournal Settings field is overridable via env."""
    from nora.config import Settings

    monkeypatch.setenv("NORA_SESSION_TRACE_MAX_STEPS", "7")
    monkeypatch.setenv("NORA_SESSION_JOURNAL_ENABLED", "false")
    monkeypatch.setenv("NORA_OPERATOR_ALIAS", "noc-night-shift")

    settings = Settings(
        _env_file=None,
        _env_file_encoding=None,
        nora_session_journal_dir=tmp_path / "sessions",
    )

    assert settings.nora_session_trace_max_steps == 7
    assert settings.nora_session_journal_enabled is False
    assert settings.nora_operator_alias == "noc-night-shift"


def test_env_example_lists_session_journal_keys_with_synthetic_values() -> None:
    """`.env.example` MUST list every new key with a sanitized placeholder.

    No real paths, IPs, MACs, hostnames, or credentials — only synthetic
    placeholders (`change-me`, `example.com`, `localhost`, `placeholder`).
    """

    env_text = ENV_EXAMPLE.read_text()
    declared = set(re.findall(r"^([A-Z][A-Z0-9_]+)\s*=", env_text, re.MULTILINE))

    expected_new_keys = {
        "NORA_SESSION_JOURNAL_DIR",
        "NORA_SESSION_TRACE_MAX_STEPS",
        "NORA_SESSION_JOURNAL_ENABLED",
        "NORA_OPERATOR_ALIAS",
    }
    missing = expected_new_keys - declared
    assert not missing, f".env.example missing SessionJournal keys: {sorted(missing)}"

    # And the same synthetic-only contract used for the rest of the file.
    offenders: list[str] = []
    for line in env_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() not in expected_new_keys:
            continue
        v = value.strip().strip('"').strip("'")
        if PRIVATE_IPV4.search(v):
            offenders.append(f"{key}: private IPv4 literal {v!r}")
        if re.search(r"(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}", v):
            offenders.append(f"{key}: MAC literal {v!r}")
    assert offenders == [], f".env.example has non-synthetic values for new keys: {offenders}"


# --- Requirement: Intervention-memory MCP Settings fields (Phase 3) ---------
#
# Three new env-driven fields, defaults per spec R8:
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
