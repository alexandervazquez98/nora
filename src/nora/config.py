"""NORA runtime configuration.

This module is the *only* place under `src/nora/` allowed to touch
`os.environ`. All other modules MUST receive a `Settings` instance via
dependency injection and read values from it.

Precedence (per `specs/secure-configuration/spec.md`):

1. Explicit init kwargs (highest)
2. `.env` file contents (DOTENV) — overrides process env for matching keys
3. Process environment variables
4. Field defaults (lowest)

`loaded_from` records which source actually supplied the configuration.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LoadSource = Literal[".env", "process_env", "defaults"]

# Prefix used to detect process-env contribution in `loaded_from`.
_PROCESS_ENV_PREFIX = "NORA_"


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

    # --- Driver layer (Phase 2 — PMP 450i driver) ---------------------------
    # Directory holding per-vendor/per-firmware OID catalog JSON files.
    # Layout: `<oid_catalogs_path>/<vendor>/<model>/<firmware>.json`.
    nora_oid_catalogs_path: Path = Path("./data/oid-catalogs/")
    # YAML inventory file consumed by `Inventory.from_yaml`.
    nora_devices_inventory_path: Path = Path("./data/devices.yaml")
    # HMAC-SHA256 signing key for catalog verification. Empty / unset
    # values fail closed: `OidCatalogRegistry.verify_all` raises
    # `CatalogVerificationError` on boot (OidCatalog-R3).
    nora_oid_catalog_signing_key: SecretStr | None = None
    # Operator override for the prompt source directory. When None, the
    # registry falls back to the packaged prompts shipped under
    # `src/nora/prompts/` (Prompt-R3).
    nora_prompts_dir: Path | None = None

    # --- Intervention memory MCP (Phase 3) -----------------------------------
    # On-disk directory of intervention JSON records. NORA reads from this
    # dir; openchat's `intervention_memory_tool` writes to it. The default
    # is relative so no production path enters the repo; the operator wires
    # the real `.22` path in `.env` (R8 / secure-configuration).
    nora_interventions_dir: Path = Path("./var/interventions/")
    # Cap on keyword-search I/O. When `search_intervention_history` is
    # called with a `keyword`, at most this many files are read; WARNING
    # logged when the cap fires (R7).
    nora_interventions_keyword_search_max_records: int = 1000
    # Cap on correlate-scan I/O. `correlate_sector_interference` walks at
    # most this many of the most-recent records (R6).
    nora_interventions_correlate_scan_limit: int = 50

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


__all__ = ["Settings", "LoadSource"]
