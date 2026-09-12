## Exploration: NetOps Persistent Memory & Correlation MCP — NORA Slice

### Current State

**NORA today** is on `main` with two Phase 2 changes archived
(`2026-09-10-phase2-session-journal` + `2026-09-11-phase2-pmp450i-driver`).
The FastMCP server in `src/nora/server.py:41` exposes a single global
`mcp = FastMCP("nora")` instance, six `@mcp.tool` functions, the
`_AutoTraceMiddleware` (lines 255-309) that already records every tool
call into the session journal, and a module-level `_sanitizer =
Sanitizer()` (line 46). Boot sequence lives in `src/nora/__main__.py`
and ends with `mcp.run(show_banner=False)`.

**The openchat prototype is already deployed and tested** on the remote
server `alex@10.53.1.22`. `deploy_v7_intervention_memory.py` and
`deploy_v8_unbiased_pre_report.py` register the
`intervention_memory_tool` (and a v2.7.0 `cambium_pmp450_tool`) into
Open WebUI's `webui.db`. The `intervention_memory_tool` writes JSON
files like `{ticket}_{ip}_{stage}_{timestamp}.json` to
`/app/backend/data/interventions/` (or
`/home/alex/openchat/data/interventions/` when the docker path is
absent). `mcp_network_example.py` is the user's reference for a separate
SSE/STDIO MCP server, but the spec's intent is to expose this from
NORA's existing `mcp` instance.

**The spec is self-contained and authoritative** for the tool contract
(`NETOPS_INTERVENTION_MEMORY_MCP_SPEC.md`). The 3 tools are:

| Tool | Purpose |
|---|---|
| `search_intervention_history(target_ip?, ticket_number?, stage?, keyword?, limit=5)` | Read-only history search across the JSON dir |
| `get_device_lifecycle_summary(target_ip)` | Aggregated lifecycle audit for one AP/SM |
| `correlate_sector_interference(tower_name, target_frequency_mhz, channel_width_mhz=20.0)` | Cross-sector co-channel + adjacent-channel conflict scan |

**No `intervention_memory` package exists in NORA today** — confirmed
`ls src/nora/` shows only `__init__.py`, `__main__.py`, `config.py`,
`core/`, `drivers/`, `llm.py`, `prompts/`, `sanitizer.py`, `server.py`.
This is a green-field slice inside the existing MCP server.

### Affected Areas

- `src/nora/server.py` — **modified**: add 3 `@mcp.tool` functions
  registered on the global `mcp` instance, in the same single-file
  `nora-mcp-server` style used by `nora_health`,
  `nora_session_get_state`, `snmp_get_pmp450i_radio_metrics`. Auto-trace
  middleware already covers invocation recording.
- `src/nora/config.py` — **extended**: add
  `nora_interventions_dir: Path` env-driven setting with sane default,
  plus optional `nora_interventions_max_record_bytes` and
  `nora_interventions_keyword_search_max_records` caps. Existing
  `extra="ignore"` (line 49) shields callers that don't set them.
- `src/nora/intervention_memory/` — **new** package
  (`__init__.py`, `models.py`, `storage.py`, `tools.py`, `exceptions.py`).
  Mirrors the `core/` and `drivers/` style: small, focused modules
  with one typed responsibility each. Lives next to `core/` so the
  import path is `nora.intervention_memory.*`.
- `openspec/specs/intervention-memory/spec.md` — **new** delta spec
  covering the 3 read-only tools, the schema, the storage contract,
  the sanitizer boundary, and the read-only guarantee.
- `openspec/specs/nora-mcp-server/spec.md` — **modified**: one
  delta scenario per new tool, plus the cross-spec reference for the
  sanitizer + redaction + auto-trace behavior.
- `tests/test_intervention_memory_models.py`,
  `tests/test_intervention_memory_storage.py`,
  `tests/test_intervention_memory_tools.py`,
  `tests/test_server_intervention_memory_tools.py` — **new**, with
  the latter following `tests/test_server_driver_tool.py` style.
- `.env.example` — **extended** with `NORA_INTERVENTIONS_DIR` (default
  `./var/interventions/`) and the optional caps. Per `.env.example`
  style: comment explaining "this points at the directory openchat's
  tool writes to; on the production server it must be the shared path".

**NOT modified** (re-verified): the existing 6 tools, the middleware,
the session journal, the driver registry, the OID catalog, or the LLM
provider layer.

### Approaches

1. **Three tools on NORA's existing FastMCP server** *(recommended)*
   - Pros: matches SCOPE.md §4.D ("Servidor FastMCP" is the LLM surface),
     zero new process to manage, auto-trace middleware already records
     every call, sanitizer wiring is one helper, single boot sequence.
     Aligns with how `nora_health`, `snmp_get_pmp450i_radio_metrics`,
     and the 4 session tools are registered today.
   - Cons: NORA's existing server is stdio by default
     (`__main__.py:72` `mcp.run(show_banner=False)`). Open WebUI
     currently attaches to openchat's external tools — wiring NORA into
     the same UI session is the orchestrator's question (see Open
     Questions).
   - Effort: Low.

2. **New separate FastMCP server alongside NORA** (mirror of
   `mcp_network_example.py`)
   - Pros: spec's reference impl is exactly this; transport choice
     (SSE vs stdio) is independent of NORA's stdio lock; if NORA's
     stdio server has a client, this can run on `:8001/sse`.
   - Cons: second boot process, second config path, second sanitizer
     instance, second set of tests. SCOPE.md §4.D does not promise
     multiple FastMCP servers. Duplicates everything NORA already
     provides. Open WebUI would need a second MCP entry.
   - Effort: Medium.

3. **Read via NORA's REST API surface instead** (FastAPI microservice
   from SCOPE.md §4.D last bullet)
   - Pros: REST is language-agnostic; dashboard + bot integrations
     can consume too.
   - Cons: spec specifically asks for MCP tools with semantic LLM
     tool-calling semantics; a REST endpoint does not become a tool
     the LLM can pick. Wrong surface.
   - Effort: High, and wrong tool.

**Decision**: Approach 1 — three tools on NORA's existing `mcp`. Spec
is consistent with the "FastMCP server" architecture choice; the
reference impl was a stand-in while the production wiring lands inside
NORA. Splitting the surface into a second server buys nothing for
this slice.

**Within Approach 1, secondary choices:**

- **Storage backend**: stdlib `json` + `pathlib` (no DB). The
  prototype uses stdlib already; NORA's `core/session_paths.py`
  atomic-write primitive is overkill for read-only consumers and is
  for files NORA *writes*. The interventions dir is owned by openchat's
  writer.
- **Filename pattern**: `{ticket}_{ip}_{stage}_{timestamp}.json` per
  spec. Read path: glob `*.json` under
  `nora_interventions_dir`, `json.load` each, parse into
  `InterventionMemoryRecord`, filter, sort, slice.
- **Sanitizer integration**: per SCOPE.md §2 and the existing
  session-journal R6 contract, every free-text string in the tool
  response goes through `Sanitizer.sanitize(...)`. The structured
  top-level fields (`stage`, `status`, `timestamp_unix`,
  `intervention_id`) are typed and bypass; the dict-shaped
  `network_equipment` and the free-text `findings_and_dictamen` /
  `recommended_action` / `record_name` pass through. Implement as a
  helper `_sanitize_record_payload(record)` so the contract is in one
  place.
- **Schema model**: Pydantic `BaseModel` mirroring spec §4 fields.
  `InterventionMemoryRecord.network_equipment` is itself a
  `NetworkEquipmentBlock` with `pre_existing_offline_subscribers` as
  `list[PreExistingOfflineSubscriber]`. Use
  `model_config = ConfigDict(extra="ignore")` so legacy v7 files
  missing fields don't fail validation; v8 files with extra fields
  don't either.
- **Tolerant read**: skip + log a warning for any file that fails
  `json.JSONDecodeError` or `ValidationError`; never let one corrupt
  file take down the whole tool (mirrors
  `_load_or_create_or_recover` from `core/session_journal.py:452-469`).
- **Concurrent-writer safety**: the dir is openchat's; NORA does not
  hold a write lock. Filename collisions are openchat's concern
  (resolved with the per-record `intervention_id` UUID6 suffix, NOT in
  the filename). NORA readers tolerate partial writes (try/except per
  file). No file-locking needed.
- **Keyword search**: implement as case-insensitive substring on the
  JSON dump of the record (matches spec; matches prototype's
  `json.dumps(data).lower()` trick). Cap by `limit` AND by a
  `nora_interventions_keyword_search_max_records` setting to bound I/O
  on a large dir.
- **No NORA writer**: SCOPE.md §4.C zero-modification rule applies;
  the openchat writer remains the source of truth. NORA's slice is
  read-only by contract.

### Recommendation

Ship `phase3-intervention-memory-mcp` as **one PR slice**, single
new capability `intervention-memory` with three read-only tools on
NORA's existing `mcp` instance. New package
`src/nora/intervention_memory/` with 5 small modules. Pydantic model
mirrors spec §4 with `extra="ignore"` for forward + backward compat
across v7/v8 prototype records. Sanitizer runs on every free-text
field; structured fields bypass. Auto-trace middleware records every
call for free — no middleware changes. Reject any proposal that adds a
write tool; writes stay in openchat's tool per the cross-system
boundary.

**Reconcile before `--propose` formalises:**

- Spec tool `search_intervention_history` includes `keyword`; prototype
  omits it. Spec wins.
- Spec schema field is `mac`; prototype v8 emits `mac_address`. Pick
  the union (`PreExistingOfflineSubscriber.mac: str | None = None`)
  with `model_config(extra="ignore")` so legacy `mac_address` records
  still parse.
- Storage path default in spec is the production server's shared dir.
  In `.env.example` default to a relative `./var/interventions/` (no
  real production path in the public template); let the operator wire
  it via `NORA_INTERVENTIONS_DIR` on `.22`.
- The spec's `interventions_dir` permission model (prototype uses
  `0o777` for the openchat writer) is OUT OF SCOPE for NORA — NORA
  reads what openchat wrote. Do not chmod.

### Risks

- **Production data leak via tool response** (severity: HIGH) — tool
  output contains `target_ip`, `pre_existing_offline_subscribers[*].ip`
  and `[*].mac`, which are private IPv4 and MAC literals — SCOPE.md
  §2 zero-leakage requires masking. **Mitigation:** every free-text
  field on every record runs through `Sanitizer.sanitize(...)` before
  serialisation; the structured top-level `intervention_id` /
  `timestamp_unix` / `stage` / `status` bypass per existing
  session-journal R6 contract. Add a property test that every
  fixture record with private IPs/MACs yields aliases in the tool
  output (mirror `tests/test_sanitizer.py` style).
- **Read-only guarantee regression** (severity: HIGH) — a future
  contributor adds a write tool. **Mitigation:** AST lint test that
  any `open(... "w")` / `Path.write_text` / `os.replace(...)` is
  absent from `src/nora/intervention_memory/`. Mirror
  `tests/test_driver_airgap.py` style.
- **Schema drift across v7/v8 records** (severity: MEDIUM) — v7
  records lack `recommended_action`, `hardware_band`, `active_*` SM
  counts; v8 records have them; some early records have `mac` vs
  `mac_address`. **Mitigation:** `extra="ignore"` on the Pydantic
  model; every field except the spec's `required` set is `Optional`;
  missing field returns `None`, not error. Add a test fixture with
  one v7 record + one v8 record, assert both parse.
- **Empty / missing data dir on first boot** (severity: MEDIUM) —
  fresh NORA install with no interventions dir. **Mitigation:** if
  `nora_interventions_dir` does not exist, return empty results with
  a `status: NO_HISTORY_FOUND` payload (matches prototype v7 line
  141). Do NOT create the dir; NORA does not own it. Surface the
  warning on `nora_health` via the existing boot log line pattern
  (`__main__.py:59-67`).
- **Corrupt / partially-written record file** (severity: MEDIUM) —
  openchat's writer crashes mid-write; NORA sees a torn file.
  **Mitigation:** per-file try/except wrapping `json.load` and
  `InterventionMemoryRecord.model_validate`; log a warning with the
  filename; skip the file. Add a unit test that a `}.invalid.json`
  file in the dir is skipped without raising.
- **Keyword search I/O blowup** (severity: LOW–MEDIUM) — operator
  asks for keyword "interference" on a 10 000-record dir; each file
  is read + parsed + `json.dumps`d. **Mitigation:** cap with a
  `nora_interventions_keyword_search_max_records` setting (default
  1000) read at boot; the keyword path reads at most that many files
  even if `limit=5` would return early. Log a warning when the cap
  kicks in.
- **Tool output size** (severity: LOW) — `get_device_lifecycle_summary`
  embeds `latest_intervention` as the full record; with v8's
  `pre_existing_offline_subscribers` list it can be a few KB. Not
  problematic, but document a soft cap in the design phase so the
  tool contract has a known upper bound (~10 KB / record).
- **Concurrent appenders** (severity: LOW) — openchat's
  `intervention_memory_tool.save_intervention_record` and
  `cambium_pmp450_tool._auto_save_memory` both write to the same dir.
  **Mitigation:** NORA readers don't write; tolerant-read (try/except
  per file) handles a torn write. The `intervention_id` UUID6 suffix
  is the openchat-side disambiguator.
- **MCP transport mismatch** (severity: MEDIUM) — NORA's boot runs
  stdio; Open WebUI today attaches via its in-process webui.db tools,
  not via MCP. Wiring NORA's stdio server into the same UI flow is
  outside this change's scope; the orchestrator must surface this
  to the user as a deployment question.
- **Coverage 85% gate** (severity: LOW) — `openspec/config.yaml:116`
  sets the threshold. **Mitigation:** the model + storage + tool
  layers each get ≥5 unit tests; the read-only guarantee gets an
  AST-style test; the sanitizer-on-read path gets a property test.
- **`pytest-asyncio` accidentally introduced** (severity: LOW) — the
  project deliberately uses `asyncio.run` wrappers
  (`tests/core/test_session_journal_property.py`). **Mitigation:**
  reject any `[tool.pytest.ini_options] asyncio_mode` change in
  `sdd-apply`.

### Open Questions / Decisions Needed

The orchestrator should bring these back to the user:

1. **Transport wiring**: NORA's boot is stdio. Open WebUI today
   discovers openchat tools via `webui.db`'s `tool` table, not via
   MCP. Does the user want NORA's MCP tools (a) exposed to Open
   WebUI by registering them as native Open WebUI tools in
   `webui.db` (mirror of the openchat tool registration pattern), or
   (b) accessed from LM Studio / a separate MCP client? The spec's
   §7 talks about Open WebUI's "External Connections → MCP Servers",
   but the production wiring today is `webui.db`. Confirm before
   `sdd-propose`.
2. **Path on production**: spec says
   `/app/backend/data/interventions/`. On `.22` is the docker
   `/app/backend/data/` path or the host
   `/home/alex/openchat/data/` path actually mounted at runtime?
   NORA's `nora_interventions_dir` default should match the
   production mount — but that is a real-infrastructure value that
   MUST NOT enter the repo. Confirm the env-var path the operator
   will set on `.22` (without committing it).
3. **Read-only contract enforcement**: is "NORA never writes" a hard
   rule with the same teeth as SCOPE.md §5 non-goal
   "auto-reparación no supervisada"? If yes, the design phase can
   add the AST-level no-write guarantee test; if no, future changes
   can ship `save_intervention_record` inside NORA. Recommend hard
   rule.
4. **Schema governance**: who owns the JSON contract going forward?
   openchat's writer is the source of truth today; NORA's
   `InterventionMemoryRecord` reads whatever openchat writes.
   Should NORA publish its Pydantic schema as the canonical contract
   (openchat adapts to NORA), or should NORA's schema be a
   read-only mirror (openchat stays authoritative)? Recommend NORA
   mirrors; the cost of a forward-compat break in NORA is high
   (every operator upgrade must coordinate with openchat).
5. **`tower_name` correlation**: prototype uses naive
   `tower_name.lower() in system_name.lower()`. Some operators name
   their APs `<tower>-<band>-<sector>` (e.g., `TWR-ISABEL-5GHZ-A`).
   Confirm the substring approach is acceptable, or whether a
   structured `tower` field should be added to future records.
6. **`limit` cap default**: spec example shows `limit=5` for search;
   `limit=50` for the correlate tool's internal scan. Confirm 5/50
   are sane production defaults or specify per-deployment overrides
   via env vars.

### Non-Goals

Explicit list of what this slice does NOT cover:

1. **Write access.** No `save_intervention_record`, no HITL-gated
   write tool, no edit/delete. All three spec tools are read-only.
   openchat's `intervention_memory_tool` keeps owning writes.
2. **HITL for memory writes.** Out of scope. The HITL rule (SCOPE.md
   §4.C) covers device-state mutations; writing a memory record is
   not a device mutation. Future phases may revisit.
3. **Schema migration of v7 → v8 records.** NORA reads both shapes
   via `extra="ignore"`. No on-disk rewrite, no "v7 → v8 converter"
   script. openchat owns the writer side of any migration.
4. **Multi-tenant / multi-tenant-isolation.** NORA's slice reads
   whatever is in `nora_interventions_dir`. Per-tenant separation is
   openchat's concern (and outside SCOPE.md §4).
5. **Network I/O from NORA to remote `.22`.** NORA never reaches
   out to the openchat server. It only reads local disk. The
   deployment topology (same machine vs NFS mount) is openchat's
   problem.
6. **New dependencies.** No new `pyproject.toml` runtime deps.
   `pydantic` is already a transitive of `pydantic-settings`;
   stdlib `json`, `glob`, `datetime` cover the rest.
7. **MCP server split.** No separate FastMCP server alongside NORA.
   The 3 tools live on NORA's existing `mcp` instance.
8. **Re-sanitization of on-disk records.** NORA does not rewrite the
   files. It sanitizes at read time per SCOPE.md §2 + session-journal
   R6 contract. The on-disk format stays as openchat wrote it.
9. **LLM tool-call routing.** Open WebUI today uses `webui.db` for
   tools, not MCP. Wiring NORA's MCP tools into Open WebUI's flow
   (whether via `webui.db` mirroring or via External Connections)
   is a deployment / ops concern, not a code concern.
10. **Schema versioning of `InterventionMemoryRecord`.** Pydantic
    `model_version` field is omitted. If future schema breaks
    require rollout coordination, the `extra="ignore"` design + a
    future `schema_version: int = 1` field is enough headroom for
    that change.

### Ready for Proposal

**Yes — proceed to `sdd-propose`.** The spec is concrete enough
(3 named tools, schema, JSON contract) that the proposal phase can
draft intent + scope without re-deriving them. The orchestrator
should:

1. Surface the **Open Questions** above to the user — especially
   #1 (MCP transport wiring on Open WebUI) and #2 (production path
   on `.22`).
2. Confirm the **read-only hard rule** (#3) before proposal locks
   the design.
3. Forecast ~600 lines of new code (5 modules × ~100 lines + ~150
   lines of tests + the delta spec + the `.env.example` change).
   This fits the 400-line PR-review budget as **one PR if** the
   package + tool surface stays under 400, otherwise **two PRs**
   splitting on storage (PR1: models + storage + sanitization) vs
   tool surface (PR2: the 3 `@mcp.tool` + server wiring + tests).
   `sdd-tasks` will decide; recommend the orchestrator ask the
   user up-front given the spec is otherwise clear.
