"""Pydantic models for NetOps intervention memory records.

The schema tolerates v7 (no `recommended_action`, no `hardware_band`,
canonical `mac`) and v8 (`recommended_action` and `hardware_band`
present, legacy `mac_address` alias) records written by openchat's
existing `intervention_memory_tool`. Per spec R1:

- `model_config = ConfigDict(extra="ignore")` drops unknown keys
  (forward-compat with future v9 schema drift).
- Every non-required field is `Optional[T] = None`.
- `stage` / `status` use `Literal[...]` so a typo at the parse boundary
  raises `ValidationError` instead of silently leaking.

The `mac` field is the canonical name; `mac_address` is the legacy v8
alias and is silently dropped (NOT promoted). A future spec extension
could add a `model_validator(mode="before")` to promote the alias; out
of scope for this slice.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# Stage literals — must match `specs/intervention-memory/spec.md` R1.
Stage = Literal[
    "PRE_DIAGNOSTIC",
    "SPECTRUM_ANALYSIS",
    "PRE_MIGRATION",
    "SAFETY_ABORT",
    "POST_MIGRATION_VERIFIED",
    "POST_INTERVENTION",
]

# Status literals — must match `specs/intervention-memory/spec.md` R1.
Status = Literal[
    "COMPLETED",
    "ABORTED",
    "ACTION_REQUIRED",
    "PENDING_VERIFICATION",
]


class PreExistingOfflineSubscriber(BaseModel):
    """One subscriber row in `pre_existing_offline_subscribers`.

    The canonical name is `mac`; v8 records write `mac_address` (legacy
    alias). `extra="ignore"` silently drops the alias without raising.
    """

    model_config = ConfigDict(extra="ignore")

    luid: Optional[int] = None
    mac: Optional[str] = None  # canonical; `mac_address` alias is ignored.
    ip: Optional[str] = None
    uptime: Optional[str] = None
    note: Optional[str] = None


class NetworkEquipmentBlock(BaseModel):
    """The `network_equipment` sub-record.

    All fields optional — v7 records may carry only `system_name` while
    v8 records carry the full radio metrics. `extra="ignore"` tolerates
    future OID additions.
    """

    model_config = ConfigDict(extra="ignore")

    target_ip: Optional[str] = None
    system_name: Optional[str] = None
    hardware_band: Optional[str] = None
    carrier_frequency_mhz: Optional[float] = None
    total_provisioned_sms: Optional[int] = None
    active_online_sms_count: Optional[int] = None
    pre_existing_offline_sms_count: Optional[int] = None
    frame_utilization_dl_pct: Optional[float] = None
    frame_utilization_ul_pct: Optional[float] = None
    pre_existing_offline_subscribers: list[PreExistingOfflineSubscriber] = Field(
        default_factory=list
    )


class InterventionMemoryRecord(BaseModel):
    """A single intervention JSON record written to `var/interventions/`.

    Required fields (per spec R1): `intervention_id`, `timestamp_iso`,
    `timestamp_unix`, `ticket_number`, `target_ip`, `stage`, `record_name`,
    `status`, `agent_name`, `network_equipment`, `findings_and_dictamen`,
    `created_at`.

    Optional fields: `recommended_action` (v7 records lack this).
    """

    model_config = ConfigDict(extra="ignore")

    intervention_id: str
    timestamp_iso: str
    timestamp_unix: int
    ticket_number: str
    target_ip: str
    stage: Stage
    record_name: str
    status: Status
    agent_name: str
    network_equipment: NetworkEquipmentBlock
    findings_and_dictamen: str
    created_at: str
    recommended_action: Optional[str] = None


__all__ = [
    "Stage",
    "Status",
    "PreExistingOfflineSubscriber",
    "NetworkEquipmentBlock",
    "InterventionMemoryRecord",
]
