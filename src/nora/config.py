"""NORA runtime configuration.

This module is the *only* place under `src/nora/` allowed to touch
`os.environ`. All other modules MUST receive a `Settings` instance via
dependency injection and read values from it.

Precedence (per `specs/secure-configuration/spec.md`):

1. Explicit init kwargs (highest)
2. `.env` file contents (DOTENV) — overrides process env for matching keys
3. Process environment variables
4. Field defaults (lowest)

`loaded_from` records which source actually supplied the configuration so the
MCP `nora_health` tool can surface it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ProviderName = Literal["lmstudio", "gemini"]
LoadSource = Literal[".env", "process_env", "defaults"]

# Prefix used to detect process-env contribution in `loaded_from`.
_PROCESS_ENV_PREFIX = "NORA_"

# Default `nora_operator_alias`. Bounded by R13 to 64 chars; the field has
# its own Pydantic constraint that the default MUST satisfy.
_DEFAULT_OPERATOR_ALIAS: str = "anonymous"


def _has_nora_env_var() -> bool:
    """Return True if any process env var in our prefix is set (non-empty)."""
    return any(os.environ.get(name) for name in os.environ if name.startswith(_PROCESS_ENV_PREFIX))


class Settings(BaseSettings):
    """NORA runtime configuration loaded from `.env` or process env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    nora_llm_provider: ProviderName = "lmstudio"
    lmstudio_api_host: str = "localhost:1234"
    lmstudio_model_id: str = "qwen2.5-7b-instruct"
    gemini_api_key: SecretStr | None = None
    gemini_model_id: str = "gemini-2.5-flash"

    # --- SessionJournal (Phase 2) -------------------------------------------
    # Per-process directory of canonical session files. Atomic writes, owner-
    # only mode. Sanitizer keeps a separate alias map per session.
    nora_session_journal_dir: Path = Path("./var/sessions/")
    # Cap on the in-memory `trace` length before oldest steps are displaced to
    # NDJSON. Operator disk budget is the only ceiling beyond this.
    nora_session_trace_max_steps: int = 50
    # Operator-controlled opt-out: when `false`, the auto-trace middleware
    # skips recording entirely and the 3 explicit tools raise
    # `JournalDisabledError`. Defaults to True so an empty value still records.
    nora_session_journal_enabled: bool = True
    # Operator identity written into `SessionState.operator_alias`. SHALL be
    # ≤ 64 chars (per R13); the field is also constrained by Pydantic.
    nora_operator_alias: str = _DEFAULT_OPERATOR_ALIAS

    loaded_from: LoadSource = "defaults"

    @model_validator(mode="before")
    @classmethod
    def _detect_loaded_from(cls, values: Any) -> Any:
        """Tag the configuration with the source that supplied it.

        `.env` always wins over process env (per spec). The detection runs in
        a `before` validator so we can short-circuit before Pydantic merges
        sources. `__init__` pre-populates `loaded_from` based on the resolved
        `_env_file` path; this validator is the fallback for callers that
        bypass `__init__`.
        """
        env_file = cls.model_config.get("env_file", ".env")
        values = dict(values) if isinstance(values, dict) else {}

        if "loaded_from" in values:
            return values

        if isinstance(env_file, str) and Path(env_file).is_file():
            if Path(env_file).stat().st_size > 0:
                values["loaded_from"] = ".env"
                return values

        if _has_nora_env_var():
            values["loaded_from"] = "process_env"
            return values

        values["loaded_from"] = "defaults"
        return values

    def __init__(self, **values: Any) -> None:
        """Detect the source of values BEFORE merging, then delegate to super.

        `BaseSettings` reads `_env_file` from its own signature and routes it to
        the dotenv source. By the time `model_validator(mode="before")` runs,
        the source has already merged in its values; the `_env_file` itself is
        no longer accessible. So we resolve the source here and pass it through
        `loaded_from` explicitly.
        """
        # If the caller passed `_env_file`, use that; otherwise fall back to
        # the model_config default (`.env`).
        env_file = values.get("_env_file")
        if env_file is None or env_file is ...:
            env_file = self.model_config.get("env_file", ".env")

        if (
            isinstance(env_file, str)
            and Path(env_file).is_file()
            and Path(env_file).stat().st_size > 0
        ):
            values.setdefault("loaded_from", ".env")
        elif _has_nora_env_var():
            values.setdefault("loaded_from", "process_env")
        else:
            values.setdefault("loaded_from", "defaults")

        # Delegate to BaseSettings. We pass through `_env_file` only when the
        # caller provided one (otherwise we let pydantic-settings apply its
        # own default, which respects `model_config["env_file"]`).
        if "_env_file" in values:
            super().__init__(**values)
        else:
            super().__init__()

    @model_validator(mode="after")
    def _check_provider_credential(self) -> "Settings":
        """Gemini MUST have its API key when active; lmstudio has no credential."""
        if self.nora_llm_provider == "gemini" and self.gemini_api_key is None:
            raise ValueError("GEMINI_API_KEY is required when NORA_LLM_PROVIDER=gemini")
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: Any,
        env_settings: Any,
        dotenv_settings: Any,
        file_secret_settings: Any,
    ) -> tuple[Any, ...]:
        """Make `.env` win over process env for matching keys.

        Default order is init → env → dotenv → secrets. The spec requires
        dotenv to beat env, so we swap the second and third entries.
        """
        return (
            init_settings,
            dotenv_settings,
            env_settings,
            file_secret_settings,
        )


__all__ = ["Settings", "ProviderName", "LoadSource"]
