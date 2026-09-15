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

    # --- Slice 4 — spectrum sweep + HITL-gated migration (PR 4) ---------------
    # Maintenance-window enforcement. ``nora_maintenance_window_minutes = 0``
    # disables enforcement (the default; tests override to exercise the
    # guard). When the value is positive the spectrum sweep tool
    # (``snmp_run_spectrum_analysis``) refuses calls outside the
    # configured window — the window starts ``nora_maintenance_window_start_minutes_ago``
    # minutes before the current clock and lasts for
    # ``nora_maintenance_window_minutes`` minutes. The two knobs let an
    # operator anchor the window to a fixed clock offset so the test
    # suite can pin "now" without sleeping.
    nora_maintenance_window_minutes: int = 0
    nora_maintenance_window_start_minutes_ago: int = 0
    # HITL rollback watchdog (commit 3). Default 300s per
    # `pmp450i-radio-tools/spec.md` sub-cluster 3 requirement "Rollback
    # Watchdog With Timeout"; tests override to a fraction of a second
    # for fast execution.
    nora_hitl_rollback_timeout_seconds: int = 300
    # HITL token TTL (commit 3). Default 900s (15 min); the operator
    # kill-switch sets ``NORA_HITL_TOKEN_TTL_SECONDS=0`` (read via
    # :func:`nora.config.hitl_kill_switch_active`) to disable HITL
    # acceptance.
    nora_hitl_token_ttl_seconds: int = 900
    # Issue #43 / `2026-09-15-3tier-tool-governance`: HMAC-SHA256 signing
    # key for HITL approval tokens. Mirrors `nora_oid_catalog_signing_key`
    # (catalog HMAC). Lazy fail-closed: empty / missing key raises
    # `AutonomousMutationRejected` at first Tier-2 invocation
    # (`mint_token` / `verify_approval_token`); Tier-0 / Tier-1 boot
    # proceeds regardless.
    nora_hitl_signing_key: SecretStr | None = None
    # Issue #43 / `2026-09-15-3tier-tool-governance`: operator-overridable
    # tool-spec directory scanned alongside the packaged prompts at boot.
    # `PromptRegistry.from_settings` reads this field; `None` (or a
    # non-existent path) skips the tool-spec dir without raising.
    nora_tool_specs_dir: Path | None = Path("docs/tool_specs")

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


def hitl_kill_switch_active() -> bool:
    """Return True when the operator has disabled HITL via the kill switch.

    The HITL approval-token verifier at ``nora.hitl.tokens`` honours an
    operational backout escape hatch: setting the
    ``NORA_HITL_TOKEN_TTL_SECONDS`` environment variable to ``0``
    disables HITL acceptance even for well-formed, unexpired tokens.

    The kill switch lives here (and reads the process environment
    directly) so the operator can rotate the gate without going
    through :class:`Settings` — the design contract is that the kill
    switch MUST work even when ``Settings`` is locked or the process
    is reading from a pinned config. :mod:`nora.hitl.tokens` is
    therefore exempt from the repo-wide ``no direct ``os.environ``
    outside the allow-list`` rule (see
    ``tests/test_config.py::test_no_os_environ_in_src_nora``); the
    helper is co-located with the ``Settings`` boundary so the
    audit trail remains in one module.
    """
    raw = os.environ.get("NORA_HITL_TOKEN_TTL_SECONDS")
    if raw is None:
        return False
    try:
        return int(raw.strip()) == 0
    except (TypeError, ValueError):
        return False


__all__ = ["Settings", "LoadSource", "hitl_kill_switch_active"]
