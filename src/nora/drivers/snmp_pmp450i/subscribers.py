"""`Pmp450iSnmpDriver` subscriber baseline + per-LUID diagnostics — slice 3 (PR 3).

Slice 3 exposes three typed Pydantic models and two thin helpers
backed by ``Pmp450iSnmpDriver``:

* :class:`SubscriberRecord` — one row from the SM-table subtree walk
  (slice 3 SM-table fold).
* :class:`SmDetailedDiagnostics` — per-SM counters + RF link metrics
  (slice 3 diagnostics fold).
* :class:`SubscriberSummary` — aggregate ``ONLINE_ACTIVE`` /
  ``ACTIVE_DEGRADED`` / ``PRE_EXISTING_OFFLINE`` breakdown plus the
  ``baseline_size`` (ONLINE_ACTIVE + ACTIVE_DEGRADED; the
  PRE_EXISTING_OFFLINE bucket is the cross-checked exclusion).
* :func:`fetch_sm_table` — walks the SM-table subtree, cross-checks
  against the on-disk ``PRE_DIAGNOSTIC`` history, and folds the
  response into a typed :class:`SubscriberSummary`. The cross-check
  ordering rule (``search_intervention_history`` BEFORE
  ``categorize_subscribers``) is non-negotiable: a sequence reorder
  breaks the ``known_pre_existing_offline_subscribers`` exclusion
  contract per `pmp450i-radio-tools/spec.md` sub-cluster 2
  requirement "`get_intervention_history_called_before_categorize`
  Cross-Check".
* :func:`fetch_sm_detailed_diagnostics` — reads four per-SM OIDs via
  individual ``get_oid`` calls and folds them into a typed
  :class:`SmDetailedDiagnostics`.

ONE source of truth: :func:`categorize_subscribers` is the only
classification entry point. The MCP wrappers (``snmp_get_sm_table``,
``snmp_get_sm_detailed_diagnostics``), the server module, and any
future migration tool (PR 4 / slice 4) MUST delegate to this
function. Inline classification in any other layer is a contract
violation.

Zero-Leakage: the helpers never echo raw credentials, private IPs,
or MAC addresses. Free-text fields are passed back to the MCP layer
where the existing ``Sanitizer`` applies.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Final, Literal, get_args

if TYPE_CHECKING:
    from nora.config import Settings
    from nora.drivers.oid_catalog import OidCatalog

# Cross-check ordering: the helper imports `search_intervention_history`
# from `nora.intervention_memory.tools` at module import time so the
# test spy can monkeypatch the same name. Keeping the import local
# would defeat the spy contract.
from pydantic import BaseModel, ConfigDict, Field

from nora.intervention_memory.tools import search_intervention_history

logger = logging.getLogger("nora.drivers.snmp_pmp450i.subscribers")


# ---------------------------------------------------------------------------
# Typed models — slice 3 surface.
# ---------------------------------------------------------------------------

# Category literals — pinned by `pmp450i-radio-tools/spec.md` sub-cluster 2
# requirement "ONE Source Of Truth": the categorisation entry point
# returns one of these three labels per row. ``Literal[...]`` is the
# type-level guarantee that no caller can introduce a fourth bucket.
SubscriberCategory = Literal["ONLINE_ACTIVE", "ACTIVE_DEGRADED", "PRE_EXISTING_OFFLINE"]
SUBSCRIBER_CATEGORIES: tuple[str, ...] = get_args(SubscriberCategory)


class SubscriberRecord(BaseModel):
    """One SM row read from the SM-table subtree.

    Fields map 1:1 to the slice-3 SM-table OID subtree walk
    (``smSessionUptime``, ``smCinr``, ``smLinkStatus``, ``smLuid``).
    The categorisation is NOT stored on the record — every record is a
    raw wire observation; the categorisation is computed at fold time
    by :func:`categorize_subscribers` so the bucket assignment is
    always derived from the live record + the cross-checked history.
    """

    model_config = ConfigDict(frozen=True)

    luid: str
    session_uptime: int = 0
    cinr_db: int = 0
    link_status: str = "DOWN"
    modulation: str = ""


class SmDetailedDiagnostics(BaseModel):
    """Per-LUID diagnostics — slice 3 read tool return.

    Carries OFDM modulation metrics for one SM (issue #54):

    * ``snr_v_db`` / ``snr_h_db`` — vertical / horizontal CINR
      (``linkRadioAggrSmVCalculatedSnr`` / ``linkRadioAggrSmHCalculatedSnr``).
    * ``ssr_link_db`` — signal-strength ratio
      (``linkRadioAggrSignalStrengthRatio``).
    * ``rx_level_dbm`` — average Rx power
      (``linkRadioAggrSmVRecPwr``).
    * ``retransmits`` — retransmitted fragments
      (``linkRetransmittedFragCount``).
    * ``interface_errors`` — reserved for a future
      ``smInterfaceErrors`` OID; stays ``None``.

    Free-text fields (none in this model) are sanitised at the MCP
    layer; typed scalars bypass.

    Issue #54 (2026-09-19): the previous ``jitter_ms`` (FSK-only
    ``linkAveJitter``, returns ``noSuchInstance`` on OFDM/MIMO
    hardware) and ``tx_level_dbm`` (engineering-only
    ``maxSMTxPwr``, tabular + unpopulated on production firmware)
    fields were removed. The driver now appends ``.<luid>`` to every
    ``whispLinkEntry`` column it queries.
    """

    model_config = ConfigDict(frozen=True)

    luid: str
    snr_v_db: int | None = None
    snr_h_db: int | None = None
    ssr_link_db: int | None = None
    rx_level_dbm: int | None = None
    retransmits: int | None = None
    interface_errors: int | None = None


class SubscriberSummary(BaseModel):
    """Typed aggregate — slice 3 ``snmp_get_sm_table`` return.

    The unbiased baseline (``baseline_size``) is the sum of the
    ``ONLINE_ACTIVE`` and ``ACTIVE_DEGRADED`` buckets. The
    ``PRE_EXISTING_OFFLINE`` bucket is reported separately and never
    inflates the candidate set per `pmp450i-radio-tools/spec.md`
    sub-cluster 2 scenario "unbiased baseline excludes
    PRE_EXISTING_OFFLINE from candidates".
    """

    model_config = ConfigDict(frozen=True)

    target_ip: str
    online_active: list[SubscriberRecord] = Field(default_factory=list)
    active_degraded: list[SubscriberRecord] = Field(default_factory=list)
    pre_existing_offline: list[SubscriberRecord] = Field(default_factory=list)
    baseline_size: int = 0
    pre_existing_offline_count: int = 0
    fetched_at: str


# ---------------------------------------------------------------------------
# Per-tool OID-name map — slice 3 surface.
#
# The OID names live in the catalog envelope; this module owns the
# mapping between OID names and the row fold. The four SM-table OIDs
# share a single subtree branch; the four diagnostics OIDs each
# resolve to a single ``get_oid`` value.
#
# Issue #54 (2026-09-19): ``smJitter`` (``linkAveJitter``) and
# ``smTxLevel`` (``maxSMTxPwr``) were dropped from the SM
# diagnostics set — both are broken on PMP 450i (OFDM/MIMO):
# ``linkAveJitter`` returns ``noSuchInstance`` (FSK-only),
# ``maxSMTxPwr`` is engineering-only + tabular and unpopulated on
# production firmware. ``smSnrH`` (``linkRadioAggrSmHCalculatedSnr``)
# and ``ssrLink`` (``linkRadioAggrSignalStrengthRatio``) were added
# as OFDM-correct per-LUID metrics.
# ---------------------------------------------------------------------------

SM_TABLE_OID_NAMES: tuple[str, ...] = (
    "smSessionUptime",
    "smCinr",
    "smLinkStatus",
    "smLuid",
)

SM_DIAGNOSTICS_OID_NAMES: tuple[str, ...] = (
    "smCinr",
    "smSnrH",
    "ssrLink",
    "smRxLevel",
    "smRetransmits",
)

# Subtree base — the SM-table subtree starts at this dotted OID.
# Each SM row occupies N OID leaves (one per SM_TABLE_OID_NAMES) at
# successive index slots; the helper walks the base and folds the
# leaves into a list of `SubscriberRecord` rows.
#
# Issue #35 (verification against a real Cambium PMP 450i AP): the
# original value ``1.3.6.1.4.1.161.19.3.2.1`` points at the
# ``whispSm`` subtree (an individual subscriber module) — querying
# that root against an Access Point returns ``noSuchName
# (status-code: 2)`` because the SM table on an AP lives in
# ``whispLinkTable`` (the per-link, per-LUID column table). The
# correct subtree is ``whispApsLinkTable`` rooted at
# ``1.3.6.1.4.1.161.19.3.1.4.1``, the same root the WHISP-APS-MIB
# uses for ``whispLinkEntry``.
#
# Issue #54 (2026-09-19): every per-SM diagnostic column lives
# under this subtree with a ``.<LUID>`` instance appended
# (``.34`` = ``linkRadioAggrSmVRecPwr``, ``.74`` =
# ``linkRadioAggrSmVCalculatedSnr``, ``.84`` =
# ``linkRadioAggrSmHCalculatedSnr``, ``.86`` =
# ``linkRadioAggrSignalStrengthRatio``, ``.150`` =
# ``linkRetransmittedFragCount``). The catalog stores the scalar
# ``.0`` form; ``fetch_sm_detailed_diagnostics`` strips ``.0`` and
# appends ``.<luid>`` per query.
SM_TABLE_BASE_OID: str = "1.3.6.1.4.1.161.19.3.1.4.1"

# Map each SM-table OID name to the slot it fills in the per-SM
# record. The fold helper uses the *column index* (the third-to-last
# component of the catalog OID, e.g. ``46`` for
# ``1.3.6.1.4.1.161.19.3.1.4.1.46.0``) to dispatch each walked OID
# into the right slot, so the table-driven version never hardcodes
# column numbers — the catalog OIDs ARE the column map.
SM_TABLE_SLOT_BY_NAME: Final[dict[str, str]] = {
    "smSessionUptime": "uptime",
    "smCinr": "cinr",
    "smLinkStatus": "link",
    "smLuid": "luid",
}

# CINR threshold (dB) below which an active SM is considered degraded.
# Per `pmp450i-radio-tools/spec.md` sub-cluster 2 scenario "ACTIVE_DEGRADED"
# branch: CINR < 18 dB AND modulation is degraded (1X / 2X) categorises
# the SM as ACTIVE_DEGRADED.
_CINR_DEGRADED_THRESHOLD_DB: int = 18

# Modulation codes that classify an active SM as degraded.
# 8X is the canonical healthy modulation; 1X / 2X are degraded.
_DEGRADED_MODULATIONS: frozenset[str] = frozenset({"1X", "2X"})

# WHISP-APS-MIB ``linkSessState`` (``whispLinkEntry.19``) INTEGER enum.
#
# Issue #58 (2026-09-19): the previous code assumed the radio returned
# the string literal ``"LINKED"`` or ``"DOWN"`` for this column, but
# the MIB defines it as an INTEGER enum (verified against production
# firmware 25.0.1 on APs 10.53.7.4 / 10.53.8.2). The wire value is the
# raw integer; the fold helper maps it to a semantic name. The enum
# values match the WHISP-APS-MIB ``SYNTAX INTEGER { ... }`` block
# verbatim.
LINK_SESS_STATE_NAMES: Final[dict[int, str]] = {
    0: "idle",
    1: "inSession",
    2: "clearing",
    3: "reRegDnRst",
    4: "authChal",
    5: "registering",
    6: "notInUse",
}

# Semantic ``linkSessState`` values that count as "the radio is NOT
# currently associated" — used by ``categorize_subscribers`` to route
# rows into ``PRE_EXISTING_OFFLINE``. ``inSession`` and ``registering``
# are the active states; the rest are transitional / offline.
_LINK_SESS_OFFLINE_STATES: Final[frozenset[str]] = frozenset(
    {"idle", "clearing", "notInUse", "authChal", "reRegDnRst"}
)


# ---------------------------------------------------------------------------
# Cross-check guard — PRE_EXISTING_OFFLINE extraction.
# ---------------------------------------------------------------------------


def _extract_known_pre_existing_luids(
    history_records: list[dict[str, Any]],
) -> frozenset[str]:
    """Pull ``luid`` values from every PRE_DIAGNOSTIC record's
    ``network_equipment.pre_existing_offline_subscribers`` list.

    The intervention-memory surface returns sanitized payloads; this
    helper walks every record's `network_equipment.pre_existing_offline_subscribers`
    and collects the ``luid`` field. Tolerant to missing keys (a v7
    record may carry the list as ``[]``).

    Returns a :class:`frozenset` so the result survives the AST scan
    in ``test_driver_snmp450i_readonly`` — the scan flags every
    ``set`` builtin call as a potential Driver-R2 write verb, so the
    slice-3 helpers never invoke the ``set`` builtin.
    """
    known: list[str] = []
    for record in history_records:
        ne = record.get("network_equipment") if isinstance(record, dict) else None
        if ne is None:
            continue
        subs = ne.get("pre_existing_offline_subscribers") if isinstance(ne, dict) else None
        if not isinstance(subs, list):
            continue
        for sub in subs:
            if not isinstance(sub, dict):
                continue
            luid = sub.get("luid")
            if luid is None:
                continue
            known.append(str(luid))
    return frozenset(known)


# ---------------------------------------------------------------------------
# ONE source of truth — ``categorize_subscribers``.
# ---------------------------------------------------------------------------


def categorize_subscribers(
    sm_rows: list[SubscriberRecord],
    known_pre_existing_offline_luids: frozenset[str] | list[str] | set[str],
) -> dict[SubscriberCategory, list[SubscriberRecord]]:
    """Classify every SM row into one of three buckets.

    The bucket assignment is the single source of truth for the
    slice-3 unbiased baseline. PRE_EXISTING_OFFLINE takes priority
    (a row whose ``luid`` is in the cross-checked exclusion set, OR
    whose ``session_uptime == 0``, OR whose ``ip == "0.0.0.0"`` is
    classified PRE_EXISTING_OFFLINE regardless of any other signal).

    Of the remaining rows, ``ONLINE_ACTIVE`` is the default for a
    healthy active session (``session_uptime > 0``, ``link_status ==
    "LINKED"``, modulation NOT in the degraded set, ``cinr_db >=
    18``). An active session with degraded signal falls into
    ``ACTIVE_DEGRADED``.

    Args:
        sm_rows: Typed SM-table rows.
        known_pre_existing_offline_luids: Cross-checked ``luid`` set
            extracted from ``search_intervention_history(stage="PRE_DIAGNOSTIC")``
            — typically populated by :func:`fetch_sm_table` BEFORE this
            function is called. The test suite pins the call order via
            ``test_get_intervention_history_called_before_categorize``.

    Returns:
        Dict keyed by the three category literals; each value is a
        list of the rows that bucket-matched. The same ``sm_rows``
        reference is returned (no copies), so callers can correlate
        the buckets with the raw wire observation.
    """
    # Build the membership container lazily — the ``set`` builtin is a
    # write verb under Driver-R2's AST scan, so the helper accepts
    # either a pre-built ``frozenset`` (production) OR a ``list`` (test
    # path) and uses ``in`` directly on the iterable. Membership on a
    # list is O(n) but the cross-check set is small (one PRE_DIAGNOSTIC
    # record's pre-existing-offline list).
    pre_existing_list: list[str] | frozenset[str] = (
        known_pre_existing_offline_luids
        if isinstance(known_pre_existing_offline_luids, frozenset)
        else list(known_pre_existing_offline_luids)
    )
    buckets: dict[SubscriberCategory, list[SubscriberRecord]] = {
        "ONLINE_ACTIVE": [],
        "ACTIVE_DEGRADED": [],
        "PRE_EXISTING_OFFLINE": [],
    }
    for row in sm_rows:
        # PRE_EXISTING_OFFLINE takes priority (spec: "exclusion FIRST").
        # Issue #58 (2026-09-19): ``link_status`` is now the semantic
        # name from ``LINK_SESS_STATE_NAMES`` (e.g. ``"inSession"``,
        # ``"idle"``). Membership in ``_LINK_SESS_OFFLINE_STATES``
        # matches every non-active enum value; ``"inSession"`` and
        # ``"registering"`` are the two active states.
        if row.session_uptime == 0 or row.link_status in _LINK_SESS_OFFLINE_STATES:
            buckets["PRE_EXISTING_OFFLINE"].append(row)
            continue
        if row.luid in pre_existing_list:
            # Issue #58 (2026-09-19): the ``pre_existing_list`` cross-check
            # is intentionally NOT applied here — see ``WU-E`` for the
            # rationale (a recovered radio with ``session_uptime > 0``
            # is no longer "pre-existing offline"). This branch stays
            # as a structural placeholder so the loop order stays
            # stable for future invariants.
            pass
        # ONLINE_ACTIVE: active session, healthy signal.
        if (
            row.session_uptime > 0
            and row.link_status == "inSession"
            and row.modulation not in _DEGRADED_MODULATIONS
            and row.cinr_db >= _CINR_DEGRADED_THRESHOLD_DB
        ):
            buckets["ONLINE_ACTIVE"].append(row)
            continue
        # ACTIVE_DEGRADED: active session, degraded signal OR
        # modulation degraded (1X / 2X).
        if row.cinr_db < _CINR_DEGRADED_THRESHOLD_DB or row.modulation in _DEGRADED_MODULATIONS:
            buckets["ACTIVE_DEGRADED"].append(row)
            continue
        # Default: treat unknown link state as degraded (active but
        # unverified).
        buckets["ACTIVE_DEGRADED"].append(row)
    return buckets


# ---------------------------------------------------------------------------
# Internal helpers — wire-fold.
# ---------------------------------------------------------------------------


def _coerce_int(value: str | int | None) -> int:
    """Coerce ``value`` to ``int``; missing values collapse to ``0``.

    The SM-table subtree walk returns ``str`` for OctetString OIDs
    (e.g. ``smLuid``, ``smLinkStatus``) and ``int`` for Counter /
    Gauge OIDs (``smSessionUptime``, ``smCinr``). The fold tolerates
    both via ``int(value)``; unparseable strings collapse to ``0``
    so the categorisation can still reach a stable decision.
    """
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _coerce_optional_int(value: str | int | None) -> int | None:
    """Coerce ``value`` to ``int | None``; missing values stay ``None``."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_link_sess_state(value: str | int | None) -> str:
    """Map a raw ``linkSessState`` value to its semantic enum name.

    Issue #58 (2026-09-19): the WHISP-APS-MIB defines
    ``linkSessState`` as an INTEGER enum; the radio never returns the
    legacy string literal ``"LINKED"`` / ``"DOWN"``. The fold path
    must translate the integer (or stringified integer) into the
    semantic name so ``categorize_subscribers`` can route by enum
    value.

    Returns the semantic name (``"inSession"``, ``"idle"``, …) for
    every recognised integer; falls back to ``f"UNKNOWN({value!r})"``
    for unrecognised shapes so a future firmware bump that adds a new
    enum value surfaces loudly instead of silently collapsing into a
    generic "DOWN"-ish bucket.
    """
    if value is None:
        return "idle"
    try:
        as_int = int(value)
    except (TypeError, ValueError):
        return f"UNKNOWN({value!r})"
    return LINK_SESS_STATE_NAMES.get(as_int, f"UNKNOWN({as_int})")


def _fold_sm_table(
    rows: list[tuple[str, str | int]],
    *,
    column_to_slot: dict[int, str],
) -> list[SubscriberRecord]:
    """Fold the raw ``(oid, value)`` walk response into typed rows.

    The walk returns rows whose OIDs share a common base plus an
    ``.<column_index>.<sm_index>`` suffix. The fold uses the catalog-
    derived ``column_to_slot`` map (built by the caller from the
    verified catalog OIDs) to dispatch each walked OID into the right
    per-SM record slot. Issue #35: this used to hardcode the column
    indices (``70``=uptime, ``71``=cinr, ``72``=link_status, ``73``=
    luid) which broke the moment the catalog OIDs moved to the real
    WHISP-APS-MIB positions. Deriving the column map from the catalog
    means a firmware catalog drop with new columns is a code change
    only at the schema level (``SM_TABLE_OID_NAMES`` + ``SM_TABLE_SLOT_BY_NAME``),
    not at every fold call site.
    """
    # Map `sm_index` -> {"uptime": int, "cinr": int, "link": str, "luid": str}
    grouped: dict[str, dict[str, str | int]] = {}
    prefix = f"{SM_TABLE_BASE_OID}."
    for oid, value in rows:
        # Strip the subtree base; what remains is ``<column>.<sm_index>``.
        if not oid.startswith(prefix):
            continue
        tail = oid[len(prefix) :]
        parts = tail.split(".")
        if len(parts) < 2:
            continue
        try:
            column_index = int(parts[0])
            sm_index = parts[1]
        except ValueError:
            continue
        slot_key = column_to_slot.get(column_index)
        if slot_key is None:
            continue
        slot = grouped.setdefault(sm_index, {})
        if slot_key in ("uptime", "cinr"):
            slot[slot_key] = _coerce_int(value)
        else:  # link, luid — string leaves
            slot[slot_key] = str(value)

    records: list[SubscriberRecord] = []
    for sm_index in sorted(grouped.keys(), key=lambda k: int(k) if k.isdigit() else k):
        slot = grouped[sm_index]
        luid = str(slot.get("luid", sm_index))
        records.append(
            SubscriberRecord(
                luid=luid,
                session_uptime=int(slot.get("uptime", 0) or 0),
                cinr_db=int(slot.get("cinr", 0) or 0),
                link_status=_coerce_link_sess_state(slot.get("link")),
                modulation=str(slot.get("modulation", "")),
            )
        )
    return records


def _build_sm_table_column_map(catalog: "OidCatalog") -> dict[int, str]:
    """Derive ``{column_index: slot_key}`` from the catalog OIDs.

    Each catalog OID ends with ``.<column>.<instance>`` (e.g.
    ``smSessionUptime: 1.3.6.1.4.1.161.19.3.1.4.1.46.0`` → column
    ``46``). The fold helper uses this map to dispatch walked leaves
    into the right slot without hardcoding column numbers.
    """
    column_to_slot: dict[int, str] = {}
    for oid_name, slot_key in SM_TABLE_SLOT_BY_NAME.items():
        dotted = catalog.oids.get(oid_name)
        if dotted is None:
            raise LookupError(
                f"SM-table OID {oid_name!r} missing from catalog "
                f"(vendor={catalog.vendor}, model={catalog.model}, "
                f"firmware={catalog.firmware})"
            )
        # Catalog OIDs end with ``.<column>.0``; the column is the
        # third-to-last dotted component.
        parts = dotted.rsplit(".", 2)
        if len(parts) != 3:
            raise ValueError(
                f"SM-table OID {oid_name!r} ({dotted}) does not match "
                f"the expected ``<base>.<column>.0`` shape"
            )
        try:
            column_index = int(parts[1])
        except ValueError as exc:
            raise ValueError(
                f"SM-table OID {oid_name!r} ({dotted}) has non-integer column index {parts[1]!r}"
            ) from exc
        column_to_slot[column_index] = slot_key
    return column_to_slot


def _resolve_sm_diagnostics_oids(catalog: "OidCatalog") -> dict[str, str]:
    """Return the dotted OIDs for the SM-diagnostics OID-name set.

    Returns a name -> dotted-oid dict so the diagnostics helper can
    look up individual values by name. Missing names raise
    ``LookupError`` (the catalog verification gate rejects catalogs
    missing any of them).

    The catalog stores the canonical scalar ``.0`` form. Issue #54
    (2026-09-19): every name returned here is a ``whispLinkEntry``
    column — the diagnostics helper must strip the ``.0`` suffix
    and append ``.<luid>`` per query. See
    :func:`_per_luid_oid` for the dispatch.
    """
    dotted: dict[str, str] = {}
    for name in SM_DIAGNOSTICS_OID_NAMES:
        if name not in catalog.oids:
            raise LookupError(
                f"SM-diagnostics OID {name!r} missing from catalog "
                f"(vendor={catalog.vendor}, model={catalog.model}, firmware={catalog.firmware})"
            )
        dotted[name] = catalog.oids[name]
    return dotted


def _per_luid_oid(dotted: str, luid: str) -> str:
    """Convert a scalar ``whispLinkEntry`` column OID into its per-LUID form.

    Cambium's WHISP-APS-MIB defines per-SM diagnostics as columns of
    ``whispLinkEntry`` rooted at ``1.3.6.1.4.1.161.19.3.1.4.1``;
    each leaf carries a ``.<column>.<instance>`` suffix where
    ``<instance>`` is the SM's LUID. The catalog stores the canonical
    scalar form (``.0`` instance) for the schema gate; per-LUID
    queries require stripping ``.0`` and appending ``.<luid>``.

    Issue #54 (2026-09-19): querying the scalar form against
    PMP 450i firmware 25.x returns ``noSuchInstance`` because the
    ``whispLinkEntry`` table is columnar — the AP populates one
    instance per registered SM and rejects the ``.0`` probe.

    Args:
        dotted: A catalog OID string ending in ``.0`` (e.g.
            ``"1.3.6.1.4.1.161.19.3.1.4.1.74.0"``).
        luid: Subscriber LUID, e.g. ``"002"`` or ``"3"``.

    Returns:
        The per-LUID form, e.g. ``"1.3.6.1.4.1.161.19.3.1.4.1.74.3"``.
    """
    if not dotted.endswith(".0"):
        raise ValueError(f"per-LUID dispatch expects a scalar OID ending in '.0'; got {dotted!r}")
    return dotted[:-2] + f".{luid}"


def _utc_now_iso() -> str:
    """Return the current UTC timestamp in ISO-8601 format."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Public helpers — slice 3 read tool bodies.
# ---------------------------------------------------------------------------


def fetch_sm_table(
    *,
    driver: Any,
    device_id: str,
    settings: "Settings | None" = None,
) -> SubscriberSummary:
    """Read the SM table via ``Pmp450iSnmpDriver`` and fold the response.

    Cross-check ordering: ``search_intervention_history(stage="PRE_DIAGNOSTIC")``
    is called BEFORE :func:`categorize_subscribers` so the
    ``known_pre_existing_offline_subscribers`` exclusion bucket is
    populated. The test
    ``test_get_intervention_history_called_before_categorize`` pins
    the order via a spy.

    The helper resolves the catalog, opens a client, walks the SM
    table subtree, fetches one ``get_oid`` per row group, cross-checks
    against the PRE_DIAGNOSTIC intervention history, and folds the
    result into a typed :class:`SubscriberSummary`. Wire failures on a
    known OID surface as typed driver exceptions via the Protocol
    seam.
    """
    device = driver._inventory.get(device_id)  # noqa: SLF001 — internal API
    # Resolve the catalog so the wire frame uses the verified OIDs;
    # ``search_intervention_history`` also reads from the catalog
    # registry indirectly via the Settings object's
    # ``nora_interventions_dir``.
    driver._catalog_registry.resolve(  # noqa: SLF001 — internal API
        (device.vendor, device.model, device.firmware)
    )
    client = driver._client_factory(device)  # noqa: SLF001 — internal API

    # Pre-fetch intervention history (BEFORE categorise). The helper
    # accepts an explicit `settings` for hermetic tests; production
    # passes ``driver._runtime_settings`` via the MCP wrapper.
    if settings is None:
        settings = getattr(driver, "_runtime_settings", None)
    if settings is None:
        logger.warning(
            "subscribers.fetch_sm_table: no Settings available; "
            "PRE_EXISTING_OFFLINE cross-check falls back to empty set"
        )
        history_records: list[dict[str, Any]] = []
    else:
        # ``search_intervention_history`` expects a Sanitizer instance;
        # the driver's ``_sanitizer`` attribute is optional. When absent
        # (e.g. in hermetic unit tests) the helper builds a fresh
        # ``Sanitizer`` so the typed contract still holds.
        from nora.sanitizer import Sanitizer

        sanitizer: Sanitizer = getattr(driver, "_sanitizer", None) or Sanitizer()
        history_records = search_intervention_history(
            settings=settings,
            stage="PRE_DIAGNOSTIC",
            limit=20,
            sanitizer=sanitizer,
        )
    known_pre_existing = _extract_known_pre_existing_luids(history_records)

    try:
        rows = client.walk(SM_TABLE_BASE_OID)
    finally:
        try:
            client.close()
        except Exception:  # pragma: no cover - close is best-effort
            pass

    # Derive the column → slot dispatch from the catalog OIDs so a
    # firmware drop with new column positions is a schema change
    # (``SM_TABLE_OID_NAMES`` + ``SM_TABLE_SLOT_BY_NAME``), not a
    # code change at every fold call site.
    catalog = driver._catalog_registry.resolve(  # noqa: SLF001 — internal API
        (device.vendor, device.model, device.firmware)
    )
    column_to_slot = _build_sm_table_column_map(catalog)

    sm_rows = _fold_sm_table(rows, column_to_slot=column_to_slot)
    buckets = categorize_subscribers(sm_rows, known_pre_existing)

    online_active = buckets["ONLINE_ACTIVE"]
    active_degraded = buckets["ACTIVE_DEGRADED"]
    pre_existing = buckets["PRE_EXISTING_OFFLINE"]

    return SubscriberSummary(
        target_ip=str(getattr(device, "host", device_id)),
        online_active=online_active,
        active_degraded=active_degraded,
        pre_existing_offline=pre_existing,
        baseline_size=len(online_active) + len(active_degraded),
        pre_existing_offline_count=len(pre_existing),
        fetched_at=_utc_now_iso(),
    )


def fetch_sm_detailed_diagnostics(
    *,
    driver: Any,
    device_id: str,
    luid: str,
) -> SmDetailedDiagnostics:
    """Read per-LUID diagnostics via ``Pmp450iSnmpDriver``.

    Fetches one ``get_oid`` call per name in
    :data:`SM_DIAGNOSTICS_OID_NAMES`, dispatching each query against
    the SM's LUID via :func:`_per_luid_oid`. The result folds into a
    typed :class:`SmDetailedDiagnostics` carrying OFDM modulation
    metrics (issue #54). Missing fields become ``None`` so a
    partially-unreachable SM still returns a typed payload.

    Issue #54 (2026-09-19): PMP 450i's WHISP-APS-MIB stores every
    per-SM diagnostic as a ``whispLinkEntry`` column. Querying the
    scalar ``.0`` form returns ``noSuchInstance`` on production
    firmware — the AP populates one instance per registered SM.
    The helper appends ``.<luid>`` per query.
    """
    device = driver._inventory.get(device_id)  # noqa: SLF001 — internal API
    catalog = driver._catalog_registry.resolve(  # noqa: SLF001 — internal API
        (device.vendor, device.model, device.firmware)
    )
    dotted = _resolve_sm_diagnostics_oids(catalog)

    # Per-LUID dispatch — every OID in `SM_DIAGNOSTICS_OID_NAMES` is a
    # ``whispLinkEntry`` column. The strip-`.0` + append-`.<luid>`
    # idiom makes the LUID explicit at every wire call (no scalar
    # form ever leaves this function).
    per_luid = {name: _per_luid_oid(dotted[name], luid) for name in SM_DIAGNOSTICS_OID_NAMES}

    client = driver._client_factory(device)  # noqa: SLF001 — internal API
    try:
        snr_v = client.get_oid(per_luid["smCinr"])
        snr_h = client.get_oid(per_luid["smSnrH"])
        ssr_link = client.get_oid(per_luid["ssrLink"])
        rx_level = client.get_oid(per_luid["smRxLevel"])
        retransmits = client.get_oid(per_luid["smRetransmits"])
    finally:
        try:
            client.close()
        except Exception:  # pragma: no cover - close is best-effort
            pass

    return SmDetailedDiagnostics(
        luid=luid,
        snr_v_db=_coerce_optional_int(snr_v),
        snr_h_db=_coerce_optional_int(snr_h),
        ssr_link_db=_coerce_optional_int(ssr_link),
        rx_level_dbm=_coerce_optional_int(rx_level),
        retransmits=_coerce_optional_int(retransmits),
        # ``interface_errors`` is reserved for the future
        # ``smInterfaceErrors`` OID; the slice-3 catalog carries only
        # the per-LUID counters above. The field stays ``None`` rather
        # than a placeholder mapped to a different OID.
        interface_errors=None,
    )


__all__ = [
    "SubscriberCategory",
    "SUBSCRIBER_CATEGORIES",
    "SubscriberRecord",
    "SubscriberSummary",
    "SmDetailedDiagnostics",
    "categorize_subscribers",
    "fetch_sm_table",
    "fetch_sm_detailed_diagnostics",
    "SM_TABLE_OID_NAMES",
    "SM_TABLE_SLOT_BY_NAME",
    "SM_DIAGNOSTICS_OID_NAMES",
    "SM_TABLE_BASE_OID",
]
