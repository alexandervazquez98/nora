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
    # WU-A (feat/multi-community-band-reboot): pre-flight community
    # validation. Default True — ``snmp_migrate_radio_frequency``
    # issues one sysDescr GET against every candidate SM using its
    # own inventory credentials BEFORE the HITL gate. Operators (and
    # the legacy test fixtures that pre-date WU-A) can opt out with
    # ``NORA_PREFLIGHT_COMMUNITY_VALIDATION=false``; the catalog
    # rollout plan recommends keeping it on so BAJ01-style multi-
    # community sectors surface a typed report instead of aborting.
    nora_preflight_community_validation: bool = True
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
    # WU-4 / PR #44 follow-up plan: gate for the `hitl_mint_token` MCP
    # admin tool. Per user decision 2026-09-17, default is True (ON)
    # so NOC operators without terminal access can mint tokens from
    # chat. Set `NORA_HITL_ADMIN_ENABLED=false` to opt out and force
    # the CLI-only minting path. Future operator-id allowlist is an
    # open follow-up; do NOT add here.
    nora_hitl_admin_enabled: bool = True
    # Issue #43 / `2026-09-15-3tier-tool-governance`: operator-overridable
    # tool-spec directory scanned alongside the packaged prompts at boot.
    # `PromptRegistry.from_settings` reads this field; `None` (or a
    # non-existent path) skips the tool-spec dir without raising.
    nora_tool_specs_dir: Path | None = Path("docs/tool_specs")
    # --- Slice 4 — spectrum sweep SET/GET poll (PR 4 / issue #62 WU-3) ---
    # Default sweep duration in seconds. The hard [1, 600] bound is
    # enforced by the validator below and by the FastMCP tool wiring
    # (snmp_run_spectrum_analysis accepts an optional override at call
    # time). Mirrors the ICMP probe config layout.
    #
    # 30s is the operator's recommended production baseline (2026-09-19)
    # for "sufficient RF sampling across unforeseen noise bursts". It also
    # covers the TDD buffer flush latency observed on physical PMP 450i
    # radios (1.5-2.5s on firmware 25.0.1) so the spectrum poll loop's
    # startup guard `elapsed >= sweep_duration_seconds` is always safely
    # larger than the radio's pre-state window.
    nora_spectrum_sweep_duration_seconds: int = 30
    # Inter-poll interval. 1.0s mirrors the Cambium WHISP-BOX-MIBV2-MIB
    # recommendation; the WU-3 helper defaults to this.
    nora_spectrum_sweep_poll_interval_seconds: float = 1.0
    # Hard upper bound on a single sweep (poll loop). Default 150s covers
    # the operator-observed Cambium PMP 450i AP "timed sector spectrum
    # analysis" cycle of ~95-105s plus re-association slack (~15s); the
    # AP initiates a sector-coordinated sweep that takes ~7x the duration
    # SET value, regardless of the parameter. SM sweeps finish in ~15s
    # and re-associate in ~20s, so 150s covers both AP and SM paths with
    # headroom. Operator-overridable per-call via
    # snmp_run_spectrum_analysis(sweep_duration_seconds=...) — note that
    # only the duration SET is overridable; the timeout stays a Settings
    # knob (the sweep_duration_seconds tool param controls the SET value,
    # not the poll timeout).
    nora_spectrum_sweep_timeout_seconds: int = 150

    # --- Issue #70 / WU-1 — post-sweep HTTP fetch + RF bin decoding ---
    # Per-attempt HTTP timeout for ``SpectrumAnalysis.xml`` GET. The
    # Cambium radio's web server is slow to start after a sweep
    # (operator observation: ~5-15s of HTTP silence); 10s per
    # attempt gives enough headroom for the slowest radio observed
    # without making the test suite slow.
    nora_spectrum_http_timeout_seconds: float = 10.0
    # Max retry attempts AFTER the initial GET. Default 5 matches
    # the issue operator's "5 reintentos con 3s de delay" baseline;
    # with the default retry delay (3s) this gives 15s of patience
    # before failing. ``0`` disables retry (one attempt only).
    nora_spectrum_http_max_retries: int = 5
    # Sleep between HTTP attempts. The Cambium radio's web server
    # needs time to reload the spectrum XML after a sweep; the
    # default 3s matches the issue operator's documented recovery
    # interval.
    nora_spectrum_http_retry_delay_seconds: float = 3.0
    # Wait between AP completion and SM XML fetch. The SMs need to
    # re-associate with the AP after the coordinated sweep; 15s is
    # the operator's production baseline (typical re-association
    # observed: 8-15s).
    nora_spectrum_sm_reassociation_timeout_seconds: float = 15.0
    # Top-N count for ``ranked_clean_frequencies``. Default 10 keeps
    # the orchestrator's "where should I migrate?" output compact;
    # operators tune via ``NORA_SPECTRUM_RANKING_TOP_N``.
    nora_spectrum_ranking_top_n: int = 10

    # --- ICMP probe (issue #61) ---------------------------------------------
    # Default probe duration when the operator does not specify one on the
    # tool call. The hard floor / ceiling is enforced by the `_validate_`
    # model validator below and by the FastMCP tool wiring (WU-1.5).
    nora_icmp_default_duration_seconds: int = 600
    # Inter-packet interval. 1.0s mirrors issue §3 default cadence.
    nora_icmp_default_interval_seconds: float = 1.0
    # ICMP echo payload size (bytes). 64 mirrors the standard `ping -s 56`
    # + 8-byte ICMP header convention.
    nora_icmp_default_packet_size_bytes: int = 64
    # Per-packet `wait_for` timeout forwarded to `UnprivilegedIcmpPinger`.
    nora_icmp_per_packet_timeout_seconds: float = 5.0
    # Hard upper bound on operator-supplied probe duration (30 min).
    nora_icmp_max_duration_seconds: int = 1800
    # Hard lower bound on operator-supplied probe duration (1 min).
    nora_icmp_min_duration_seconds: int = 60
    # PR2 (issue #61 WU-2.6): directory where the atomic JSON
    # snapshots of completed probe runs land. Mirrors
    # ``nora_interventions_dir`` (one-way write for OpenChat to
    # consume). Production path: ``/var/lib/nora/probes/``.
    nora_probe_results_dir: Path = Path("./var/probes/")

    loaded_from: LoadSource = "defaults"

    @model_validator(mode="after")
    def _validate_icmp_duration_bounds(self) -> Settings:
        """Enforce `min <= default <= max` on the ICMP duration triplet.

        Issue #61 §3 caps probe duration at 30 min; the validator fails
        closed on operator-supplied misconfigurations before the engine
        (WU-1.1) or the FastMCP tool wiring (WU-1.5) ever read them.
        """
        if self.nora_icmp_min_duration_seconds > self.nora_icmp_default_duration_seconds:
            raise ValueError(
                f"nora_icmp_min_duration_seconds "
                f"({self.nora_icmp_min_duration_seconds}) must be <= "
                f"nora_icmp_default_duration_seconds "
                f"({self.nora_icmp_default_duration_seconds})"
            )
        if self.nora_icmp_default_duration_seconds > self.nora_icmp_max_duration_seconds:
            raise ValueError(
                f"nora_icmp_default_duration_seconds "
                f"({self.nora_icmp_default_duration_seconds}) must be <= "
                f"nora_icmp_max_duration_seconds "
                f"({self.nora_icmp_max_duration_seconds})"
            )
        return self

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

    @model_validator(mode="after")
    def _validate_spectrum_http_settings(self) -> "Settings":
        """Bound the post-sweep HTTP settings to documented ranges.

        Issue #70 / WU-1: keeps the spectrum helper fail-closed at
        boot — a misconfigured ``NORA_SPECTRUM_HTTP_TIMEOUT_SECONDS=0``
        or ``NORA_SPECTRUM_RANKING_TOP_N=10000`` is caught BEFORE the
        first sweep rather than silently misbehaving. Mirrors
        :meth:`_validate_spectrum_sweep_duration_bounds`.

        Bounds (enforced; values outside raise ``ValueError``):
          * ``nora_spectrum_http_timeout_seconds`` — [1.0, 60.0]
          * ``nora_spectrum_http_max_retries``     — [0, 20]
          * ``nora_spectrum_http_retry_delay_seconds`` — [0.1, 30.0]
          * ``nora_spectrum_sm_reassociation_timeout_seconds`` — [1.0, 60.0]
          * ``nora_spectrum_ranking_top_n``        — [1, 100]
        """
        http_timeout = float(self.nora_spectrum_http_timeout_seconds)
        if http_timeout < 1.0 or http_timeout > 60.0:
            raise ValueError(
                f"nora_spectrum_http_timeout_seconds={http_timeout} "
                f"is outside the documented [1.0, 60.0] range; "
                f"see Settings.nora_spectrum_http_timeout_seconds"
            )
        http_retries = int(self.nora_spectrum_http_max_retries)
        if http_retries < 0 or http_retries > 20:
            raise ValueError(
                f"nora_spectrum_http_max_retries={http_retries} "
                f"is outside the documented [0, 20] range; "
                f"see Settings.nora_spectrum_http_max_retries"
            )
        retry_delay = float(self.nora_spectrum_http_retry_delay_seconds)
        if retry_delay < 0.1 or retry_delay > 30.0:
            raise ValueError(
                f"nora_spectrum_http_retry_delay_seconds={retry_delay} "
                f"is outside the documented [0.1, 30.0] range; "
                f"see Settings.nora_spectrum_http_retry_delay_seconds"
            )
        sm_reassoc = float(self.nora_spectrum_sm_reassociation_timeout_seconds)
        if sm_reassoc < 1.0 or sm_reassoc > 60.0:
            raise ValueError(
                f"nora_spectrum_sm_reassociation_timeout_seconds={sm_reassoc} "
                f"is outside the documented [1.0, 60.0] range; "
                f"see Settings.nora_spectrum_sm_reassociation_timeout_seconds"
            )
        ranking_top_n = int(self.nora_spectrum_ranking_top_n)
        if ranking_top_n < 1 or ranking_top_n > 100:
            raise ValueError(
                f"nora_spectrum_ranking_top_n={ranking_top_n} "
                f"is outside the documented [1, 100] range; "
                f"see Settings.nora_spectrum_ranking_top_n"
            )
        return self

    @model_validator(mode="after")
    def _validate_spectrum_sweep_duration_bounds(self) -> "Settings":
        """Bound ``nora_spectrum_sweep_duration_seconds`` to the documented [1, 600] range.

        Issue #62 / WU-3: the FastMCP tool wiring accepts an optional
        ``sweep_duration_seconds`` override at call time, but the
        stored default and the runtime override must BOTH land in
        ``[1, 600]`` so the helper never SETs a duration outside the
        Cambium WHISP-BOX-MIBV2-MIB's supported range. Mirrors the
        ICMP probe ``_validate_icmp_duration_bounds`` pattern.

        Raising here keeps the contract fail-closed: a misconfigured
        boot (``NORA_SPECTRUM_SWEEP_DURATION_SECONDS=0``) is caught
        before the first sweep rather than silently emitting an
        out-of-range SET frame against ``.220.0``.
        """
        duration = int(self.nora_spectrum_sweep_duration_seconds)
        if duration < 1 or duration > 600:
            raise ValueError(
                f"nora_spectrum_sweep_duration_seconds={duration} "
                f"is outside the documented [1, 600] range; "
                f"see Settings.nora_spectrum_sweep_duration_seconds"
            )
        return self

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
