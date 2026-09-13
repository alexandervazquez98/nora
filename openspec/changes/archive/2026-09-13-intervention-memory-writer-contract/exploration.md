## Exploration: Intervention Memory Writer Contract (Save Tool) — NORA Slice

### Current State

**Read-only invariant in force.** The `intervention_memory` package
ships with R2 (hard read-only rule) declared in
`src/nora/intervention_memory/__init__.py:3-8` and enforced structurally
by `tests/intervention_memory/test_no_writes.py:202-217`. The AST scan
walks every `.py` under `src/nora/intervention_memory/`, fails on
`open(... "w"|"a"|"x"|"+")`, `Path.write_text`, `Path.write_bytes`,
`Path.unlink`, `os.replace`, `os.remove`, `os.removedirs`,
`os.makedirs`, `shutil.rmtree`, AND on the banned imports
`pytest`, `_pytest`, `monkeypatch`, `nora.server`, `nora.drivers`,
`nora.intervention_memory.shim_webui`. A self-test
(`test_injected_write_text_call_is_detected`, lines 310-362) copies the
package to a tmp dir, injects `Path('/tmp/x').write_text('x')`, and
asserts the detector catches it. Phase 3 archived
`2026-09-11-phase3-intervention-memory-mcp` (proposal §8 Q3) explicitly
locks R2 as a "HARD RULE" with AST-level teeth.

**Writer authority today = OpenChat's `intervention_memory_tool`** (the
prototype `deploy_v7_intervention_memory.py` /
`deploy_v8_unbiased_pre_report.py`) plus `cambium_pmp450_tool._auto_save_memory`
on the same shared `NORA_INTERVENTIONS_DIR`. OpenChat's filename
convention per phase-3 explore (`openspec/changes/archive/2026-09-11-phase3-intervention-memory-mcp/explore.md:122-127`)
is `{ticket}_{ip}_{stage}_{timestamp}.json` — the `INT-` prefix appears
ONLY inside the JSON as the `intervention_id` field, NEVER in the
filename. The pinned contract regression test
`tests/intervention_memory/test_openchat_writer_contract.py:33-69`
locks in the `network_equipment.carrier_frequency_mhz` field convention
for POST_MIGRATION_VERIFIED records. Issue #10 already shipped that
regression test (commit `783ef34` "test(intervention_memory): lock
OpenChat writer contract + ops docs").

**Storage contract already partial-tolerant.** `src/nora/intervention_memory/storage.py:32-52`
catches `OSError`, `json.JSONDecodeError`, and `pydantic.ValidationError`
per file, logs a WARNING (`_WARNING_FORMAT` at line 29), and returns
`None` so the next file proceeds. **The reader does NOT quarantine
partial writes** — a torn file is silently skipped, the operator must
inspect the WARNING log line and remove the bad file themselves.

**No atomic-write helper exists.** No `tmp + os.replace` primitive
ships in NORA today (grep confirms zero hits in `src/nora/`). The
phase-3 proposal §6 cites "NORA's `core/session_paths.py` atomic-write
primitive is overkill for read-only consumers" — that primitive was
never actually written; phase 3 dropped `core/` entirely in the thin
split (`2026-09-12-nora-mcp-thin-split` commit `6e2d1e5`). Today every
on-disk JSON write in NORA's test suite uses `Path.write_text(...)`
directly with no fsync.

**Sanitization boundary = READ-side only.** `src/nora/intervention_memory/sanitize.py:43-63`
walks an already-parsed `InterventionMemoryRecord` through
`Sanitizer.sanitize(...)` on tool output. The walker has a bypass set
(`_BYPASS_FIELDS` at lines 29-40) for typed scalars
(`intervention_id`, `target_ip`, `stage`, `status`, `timestamp_unix`,
`timestamp_iso`, `created_at`, `ticket_number`). **No sanitizer path
exists for INCOMING payloads** — anything the writer receives lands
on disk verbatim. Issue #12's writer contract does not specify
whether credentials pasted into `findings_and_dictamen` get masked
before write.

**`Settings` already carries the dir.** `src/nora/config.py:66` defines
`nora_interventions_dir: Path = Path("./var/interventions/")` with env
override `NORA_INTERVENTIONS_DIR`. The new writer would NOT need new
settings unless it introduced a new cap (max-record-bytes, allowed
agent-name allow-list, etc.); currently no new env var is implied.

**Issue #12 explicitly says NO HITL on the write.** The body lists
security considerations as: (a) path-traversal guard inside
`NORA_INTERVENTIONS_DIR`, (b) filename format
`INT-<ticket>-<ip>-<unix>-<hex>.json` sanitised on `ticket_number` and
`timestamp_unix`. **HITL is mentioned only in issue #15 for
`snmp_migrate_radio_frequency` (Make-Before-Break)** — confirmed by
`gh issue view 15` body which carries the HITL rule for the destructive
RF migration, NOT for record-keeping. The risk model distinguishes:

- **#12 write**: operator mistakes a record → recoverable by deleting
  the file (they own the dir per SCOPE.md §4.C).
- **#15 migration**: destructive RF change to a live AP → HITL-gated.

The blast-radius asymmetry means the writer does NOT need HITL.
Recommendation: confirm with the user in proposal phase, but design as
NO-HITL.

### Affected Areas

- `src/nora/intervention_memory/__init__.py` — **modified**: the
  hard-read-only docstring (R2) gets an ADDENDUM that points to the
  sibling writer package; do NOT loosen the rule, do NOT add
  `__all__` re-exports of the writer (one-way dep stays: writer →
  reader for schema, never reader → writer).
- `src/nora/intervention_memory/models.py` — **unmodified**: the
  writer imports `InterventionMemoryRecord` for `model_validate`
  BEFORE write. The reader-side schema tolerance
  (`extra="ignore"`) is unaffected; the writer validates the SAME
  schema and rejects payloads that don't parse.
- `src/nora/intervention_memory/storage.py` — **modified, read-side
  unchanged**: the new writer depends on `Settings.nora_interventions_dir`
  and `Path` resolution only — does NOT import `read_records`. The
  reader's tolerant-glob stays exactly as is. The writer is a SEPARATE
  module so the AST guard's writer-exception logic never touches
  `storage.py`.
- `src/nora/intervention_memory/tools.py` — **unmodified**: the three
  library functions stay read-only. The writer is a NEW library
  function in the new writer package, never merged into `tools.py`
  (merging would require relaxing the AST guard; we don't).
- `src/nora/server.py` — **modified, +1 `@mcp.tool` wrapper**: the
  existing 4 tool registrations stay; the writer's MCP wrapper is a
  5th `@mcp.tool` that delegates 1:1 to the writer's library
  function (mirroring the read-only wrappers' shape at lines 163-253).
  Adds ~25 LOC + 1 entry in `__all__`. `_SERVER_INSTRUCTIONS`
  (line 55) gets one sentence advertising the writer.
- `src/nora/config.py` — **extended** (optional): add at most ONE
  new setting `nora_interventions_max_record_bytes: int` (default
  64 KB) to bound the writer's payload size. Optional because
  issue #12 doesn't ask for it; design phase decides. Existing
  `extra="ignore"` (line 43) keeps callers source-compatible.
- `src/nora/intervention_writer/` — **NEW package**
  (`__init__.py`, `writer.py`, `filenames.py`, `mcp_bridge.py`)
  parallel to `src/nora/intervention_memory/`. Mirrors the
  `core/`, `drivers/` style: small, focused modules. Three modules:
    - `__init__.py`: docstring declaring the writer invariant
      ("writer owns writes to `nora_interventions_dir`; reader
      package stays read-only; one-way dep: writer → reader.models
      + reader.storage (path resolve only)"); re-exports
      `save_intervention_record`.
    - `writer.py`: pure logic — `save_intervention_record(settings,
      payload: dict) -> dict` that validates against
      `InterventionMemoryRecord`, sanitizes incoming free-text
      fields (defense in depth), builds the filename, and atomically
      writes `tmp + os.replace`. Catches `OSError` → returns
      `{"status": "WRITE_ERROR", "error_class": ..., "message":
      sanitized}` (NEVER raises to the MCP client).
    - `filenames.py`: pure function `build_filename(payload) -> str`
      that derives `INT-<sanitised-ticket>-<sanitised-ip>-<unix>-<6-hex>.json`,
      with strict regex validation rejecting `..`, `/`, NUL,
      whitespace; raises a typed `InvalidFilenameComponent` on
      violation.
    - `mcp_bridge.py`: thin `@mcp.tool` wrapper that delegates to
      `writer.save_intervention_record`; lives here (not in
      `src/nora/server.py`) so the writer package is self-contained
      and a future change can swap the MCP surface without touching
      the main server file. **Alternative**: register the tool from
      `src/nora/server.py` (mirroring the read-only tools); cheaper
      but couples writer surface to the main server file. Design
      phase decides which one.
- `src/nora/intervention_memory/shim_webui.py` — **unmodified**: the
  webui.db shim stays read-only. If openchat wants a writer shim
  later, that's a new file in the writer package; not in scope here.
- `tests/intervention_memory/test_no_writes.py` — **unmodified**: the
  AST guard scans `src/nora/intervention_memory/` only. The new
  writer lives under `src/nora/intervention_writer/` and is OUTSIDE
  the guard's scope. The guard's R2 hard rule still applies to the
  READER package. Add a NEW test file
  `tests/intervention_writer/test_writer_contract.py` that covers
  writer-specific concerns (path-traversal guard, atomic write,
  sanitization on write, schema rejection, filename collision).
- `tests/test_server.py` — **modified, +1 test**: confirms
  `save_intervention_record` is registered on the global `mcp`
  instance (mirrors existing test for the 4 read-only tools).
- `tests/intervention_writer/` — **NEW directory**: 3-4 test files
  covering `writer.py`, `filenames.py`, `mcp_bridge.py` (if it
  lives in the writer package), and the path-traversal guard.
- `openspec/specs/intervention-memory/spec.md` — **modified**:
  delete the "no write tool" implicit rule from the existing R2/R9
  scenarios (the rule was: writer is openchat's; that is no longer
  true); add a cross-reference to the NEW capability spec
  `openspec/specs/intervention-writer/spec.md`. The reader spec
  keeps R2 (the reader stays read-only) but acknowledges the
  writer lives in a sibling package.
- `openspec/specs/intervention-writer/spec.md` — **NEW capability**:
  7-8 requirements covering the writer contract (filename,
  path-traversal, atomic write, sanitization-on-write, schema
  validation, MCP wrapper shape, banned-imports boundary).
- `openspec/specs/nora-mcp-server/spec.md` — **modified**: one
  delta scenario for the 5th tool registration; cross-reference
  to the new `intervention-writer` capability.
- `.env.example` — **modified, optional**: if the design phase
  introduces `nora_interventions_max_record_bytes`, add one line.
  Otherwise no change.

**NOT modified** (re-verified): the existing 4 MCP wrappers, the
`_ToolLogMiddleware`, the OID catalog, the LLM provider layer (dropped
in thin split), the PMP 450i driver, the prompt registry. The shim
stays read-only. The reader package's AST guard stays in force on the
reader package only.

### Approaches

1. **NEW sibling package `nora.intervention_writer/`** *(recommended)*
   - Pros: R2 hard-read-only rule stays at 100% strength on the reader
     package (zero structural change). The writer has its own
     AST-guard-style test that asserts the writer DOES have the
     expected write calls (positive assertion — opposite polarity).
     One-way dep: writer → reader.models + reader.storage (path
     resolution helper, not `read_records`). Reusable from library
     code (#15's `snmp_get_ap_summary` can call `save_intervention_record`
     after a successful diagnostic). Mirrors Phase 3's source-of-truth
     + wrappers pattern. The new package ships its own `__init__.py`
     docstring declaring the writer invariant, separate from R2.
     Filename collision risk is bounded because the writer's filename
     template (`INT-...`) is unambiguous and the writer refuses to
     overwrite an existing file (it returns `{"status":
     "DUPLICATE_INTERVENTION_ID"}` so the caller can pick a new hex
     suffix).
   - Cons: ~50 LOC of new module scaffolding (`__init__.py`,
     `writer.py`, `filenames.py`, `mcp_bridge.py-or-server.py-edit`,
     3-4 test files). One extra directory to discover in
     `tests/`. The cross-spec reference adds ~5 LOC to the reader
     spec.
   - Effort: Medium.

2. **Writer code lives in `src/nora/server.py`** (escapes the
   reader's AST guard by living outside the reader package)
   - Pros: ~25 LOC of new code in `server.py` (a 5th `@mcp.tool`
     function + helper). Zero new package scaffolding. The reader
     AST guard is untouched. Single MCP surface file to audit.
   - Cons: Conflates the read-only MCP surface with the writer
     surface (the 5th tool breaks the existing read-only invariant
     declared in the `_SERVER_INSTRUCTIONS` string at server.py:55).
     The writer library function is NOT exposed for #15 library
     reuse — `snmp_get_ap_summary` cannot auto-save a record without
     calling the MCP wrapper, which is wrong (MCP wrappers are
     stdio surface, library code should call library functions).
     No isolated unit tests for the writer (it'd be tested only
     through MCP integration).
   - Effort: Low.

3. **Writer code lives in `src/nora/intervention_memory/` and the
   AST guard is relaxed for one module** (e.g., whitelist
   `writer.py` as an exception)
   - Pros: All writer code in one place; reader + writer share
     `models.py` and `Settings` cleanly. No cross-package import.
   - Cons: **REGRESSES R2.** The hard-read-only rule was designed
     as a structural invariant. Whitelisting one module is the
     first step toward "just this once" exceptions that compound.
     The AST guard's existing self-test
     (`test_injected_write_text_call_is_detected`) would need to
     skip `writer.py`; that test's poison-injection logic becomes
     ambiguous ("does the detector skip the exception or catch
     it?"). The reader package's mental model ("everything here
     is read-only") breaks. **Reject this approach.**
   - Effort: Low, but design debt = High.

**Decision**: Approach 1 — new sibling package
`src/nora/intervention_writer/`. R2 stays at 100% on the reader; the
writer is a NEW invariant declared in its own package docstring; the
one-way dep (writer → reader.models) keeps the reader free of any
     import back. The cost is ~50 LOC of scaffolding; the benefit is
     that the reader package cannot regress its read-only guarantee
     by a future writer change.

**Within Approach 1, secondary choices:**

- **Filename template**: `INT-<sanitised-ticket>-<sanitised-ip>-<unix>-<6-hex>.json`
  per issue #12. `<sanitised-ticket>` strips any character outside
  `[A-Za-z0-9_-]` (rejects `/`, `..`, NUL, whitespace, unicode). The
  reader's existing glob is `*.json` — it does NOT match by filename
  pattern, so the writer's filename does NOT need to match the
  reader's contract. OpenChat's `{ticket}_{ip}_{stage}_{timestamp}.json`
  filenames continue to coexist because the reader's tolerant glob
  picks them up regardless. **No collision risk** in practice
  because the writer refuses to overwrite (`DUPLICATE_INTERVENTION_ID`)
  and the reader never writes.
- **Collision policy with OpenChat's existing files**: the writer
  enforces `intervention_id` uniqueness INSIDE the new package's
  namespace by the 6-hex suffix (per `os.urandom(3).hex()`). Two
  writers producing the same `intervention_id` is impossible by
  construction (the suffix is fresh per call). OpenChat's writer
  produces a different filename prefix (`{ticket}_...` not `INT-...`),
  so cross-writer collisions on disk are vanishingly unlikely. The
  writer's collision-check is `os.path.exists(target_path)` →
  retry-with-new-suffix up to 5 times → `DUPLICATE_INTERVENTION_ID`.
- **Atomic write primitive**: write to `target_path.with_suffix('.json.tmp')`,
  `os.fsync(tmp.fileno())`, then `os.replace(tmp, target_path)`. The
  reader's tolerant-storage already handles a torn tmp file (the
  per-file try/except in `storage.py:38-52` catches `OSError` +
  `JSONDecodeError`), so a crash between write and replace leaves a
  `.tmp` file the reader skips (no `.json` suffix match). Cleanup
  on the next writer call sweeps `*.json.tmp` older than 1 hour.
- **Sanitization on write**: the writer runs incoming free-text fields
  (`record_name`, `findings_and_dictamen`, `recommended_action`,
  `agent_name`, `network_equipment.system_name`,
  `network_equipment.hardware_band`,
  `network_equipment.pre_existing_offline_subscribers[*].note`) through
  `Sanitizer.sanitize(...)` BEFORE serialisation, mirroring the
  reader's `_BYPASS_FIELDS` set. **Rationale**: defense in depth. If
  the operator pastes a credential into `findings_and_dictamen`, the
  on-disk file is already masked. A future reader that forgets to
  sanitize on read still gets a safe file. **Trade-off**: the
  writer does NOT mask `target_ip` (matches R9 bypass list — typed
  scalar, sanitizing it would break the reader's correlate logic).
- **Schema validation on write**: the writer calls
  `InterventionMemoryRecord.model_validate(payload)` BEFORE write.
  Pydantic raises `ValidationError` on bad `stage` / `status` literals
  or missing required fields; the writer catches it and returns
  `{"status": "INVALID_PAYLOAD", "errors": [...]}` (sanitized errors,
  not raw Pydantic messages which can leak field paths).
- **MCP wrapper surface**: register the writer `@mcp.tool` from
  `src/nora/server.py` (mirroring the read-only tools' shape at
  lines 163-253) OR from the writer's own `mcp_bridge.py` (which
  imports the global `mcp` instance). Recommend `src/nora/server.py`
  because: (a) the existing thin server contract says all tools
  register there, (b) the `_ToolLogMiddleware` records the call
  automatically, (c) `_SERVER_INSTRUCTIONS` gets one sentence
  advertising the writer. The writer library function in
  `writer.py` stays callable from non-MCP code (#15 reuse). This is
  the same source-of-truth + wrappers pattern as Phase 3.
- **Path-traversal guard**: `target_path.resolve()` MUST start with
  `Path(settings.nora_interventions_dir).resolve()` — strict
  containment check. The writer refuses any payload where
  `build_filename()` returns a path that resolves outside the
  interventions dir. Unit test: `intervention_id="INT-../../../etc/passwd-1-123-XYZ"`
  is rejected with `PATH_TRAVERSAL_DETECTED`.
- **No HITL**: confirmed against issue #15 (HITL lives there for
  destructive RF changes). The writer is record-keeping; the
  blast-radius is "operator mistakes a record", recoverable by
  deleting the file. **No HITL gate.** Design phase confirms with
  user but the recommendation is firmly NO-HITL.

### Recommendation

Ship `2026-09-13-intervention-memory-writer-contract` as **Approach 1**:
new sibling package `src/nora/intervention_writer/` with 3 small modules
(`__init__.py`, `writer.py`, `filenames.py`) plus a thin MCP wrapper in
`src/nora/server.py` (mirrors the existing 4 read-only tool wrappers).
R2 stays at 100% on the reader package — the AST guard in
`tests/intervention_memory/test_no_writes.py` is NOT touched; it
continues to scan `src/nora/intervention_memory/` and to assert zero
writes there. The new writer package has its own positive-polarity
test that asserts the writer DOES have the expected write calls
(so a future regression that accidentally strips the write also
fails the build). Filename template `INT-<ticket>-<ip>-<unix>-<hex>.json`
per issue #12, with strict regex sanitization on `<ticket>` and
`<ip>` components rejecting path-traversal chars. Atomic write via
`tmp + fsync + os.replace` (the `.tmp` file is naturally skipped by the
existing reader's `*.json` glob). Incoming payloads are sanitized
defense-in-depth before serialisation so the on-disk file is
self-masked. Schema is validated via
`InterventionMemoryRecord.model_validate(payload)`; bad payloads return
`{"status": "INVALID_PAYLOAD", "errors": [...]}` with sanitized
errors (no raw Pydantic messages). NO HITL — confirmed against issue
#15; the writer is record-keeping, blast-radius is recoverable.

**Reconcile before `--propose` formalises:**

- **Filename collision with OpenChat's existing files**: minimal
  risk in practice because OpenChat uses `{ticket}_...` not `INT-...`.
  The reader's `*.json` glob picks up both. Confirm in proposal.
- **Filename regex strictness**: the strict `[A-Za-z0-9_-]+` filter on
  `<ticket>` and `<ip>` may reject legitimate OpenChat-style ticket
  numbers with `/` or `.`. Confirm the regex in proposal.
- **Sanitization on write vs read**: defense-in-depth means the on-disk
  file is already masked. A future operator who manually edits the
  file with unmasked data needs to re-sanitize; document this in
  OPERATIONS.md.
- **`Settings.nora_interventions_max_record_bytes`**: issue #12
  doesn't ask for it. Skip for v1; revisit in design phase if the
  reader's `R6` keyword I/O cap needs a counterpart.

### Risks

- **Filename collision with OpenChat's existing files** (severity:
  LOW-MEDIUM) — OpenChat writes `{ticket}_{ip}_{stage}_{timestamp}.json`;
  NORA's writer emits `INT-<ticket>-<ip>-<unix>-<hex>.json`. Different
  filename templates, same shared `NORA_INTERVENTIONS_DIR`. The
  writer's 6-hex suffix makes collision impossible within the writer
  namespace; cross-writer collision on disk is unlikely because the
  filename prefixes differ. **Mitigation:** the writer's
  `DUPLICATE_INTERVENTION_ID` guard retries up to 5 times with fresh
  hex before failing; reader's tolerant glob does not care about
  prefix.
- **Partial-write recovery** (severity: MEDIUM) — process killed
  between `write_text` and `os.replace`; a torn `.tmp` file is left
  on disk. **Mitigation:** atomic `tmp + fsync + os.replace` is the
  only contract. The reader's `*.json` glob SKIPS `.tmp` files
  naturally (suffix mismatch). A startup sweep in
  `writer.save_intervention_record` removes `*.json.tmp` older than
  1 hour (use `Path.stat().st_mtime`). Unit test: simulate a torn
  `.tmp` file with `{not json` content and assert the reader skips
  it AND the next writer call removes it.
- **Malicious MCP client writing junk** (severity: HIGH) — a
  compromised MCP client calls `save_intervention_record` with a
  100-MB payload, or with `intervention_id` containing path
  traversal, or with a forged `agent_name`. **Mitigation:** (a)
  strict regex on `intervention_id` rejects path-traversal chars;
  `target_path.resolve()` containment check is the second line;
  (b) `nora_interventions_max_record_bytes` cap (if design phase
  adds it, default 64 KB) rejects oversized payloads; (c) `stage`
  and `status` Literals reject typos at the parse boundary; (d)
  `intervention_id` length cap (256 chars). Path-traversal test:
  `intervention_id="INT-../../../etc/passwd-1-123-XYZ"` →
  `PATH_TRAVERSAL_DETECTED`. Oversize test: 100 MB → `PAYLOAD_TOO_LARGE`.
- **Schema validation drift** (severity: MEDIUM) — writer and reader
  use the SAME `InterventionMemoryRecord` schema (writer imports
  from `nora.intervention_memory.models`). If a future schema
  change adds a required field, the writer rejects the new field as
  a missing required on existing payloads; the reader silently
  parses because `extra="ignore"` drops unknowns. **Mitigation:**
  one-way dep writer → reader.models means schema changes touch
  both; the change is atomic. The reader's existing R1
  `extra="ignore"` + `Optional[T] = None` discipline keeps it
  forward-compatible. Schema-drift test: writer accepts a v9 record
  with an unknown field (writer drops unknown with `extra="ignore"`
  via Pydantic); reader's existing `test_models.py` proves the
  tolerance. Document the contract in the new spec.
- **Sanitization-on-write cost** (severity: LOW) — every write runs
  the sanitizer walker over the payload. Same cost as a read, plus
  one `fsync`. Throughput: ~100 records/sec on commodity SSD with
  fsync; well above realistic operator write rate (single-digit per
  minute). **Mitigation:** no action needed; benchmark only if a
  future change reports slowness.
- **AST guard regression on the reader** (severity: LOW) — a future
  contributor accidentally adds `Path.write_text` to
  `intervention_memory/storage.py`. **Mitigation:** the AST guard
  is untouched in this change; the existing
  `test_injected_write_text_call_is_detected` self-test still
  proves the detector catches it. No new risk.
- **MCP transport mismatch** (severity: MEDIUM, inherited) — NORA's
  boot is stdio (`__main__.py:72`). Issue #12 mentions
  Streamable HTTP / SSE clients writing records; NORA's stdio
  server doesn't expose that transport today. **Mitigation:** the
  writer library function in `writer.py` is callable from ANY
  transport (stdio, SSE, Streamable HTTP, library code); the MCP
  wrapper is one of many possible surfaces. Out of scope here to
  add a new transport — that's a deployment concern. Confirm with
  the user in proposal phase.
- **Coverage 85% gate** (severity: LOW) — `openspec/config.yaml:116`
  sets the threshold. The writer package's 3 modules × ~80 LOC
  + 4 test files × ~80 LOC = ~560 LOC added. **Mitigation:** every
  writer module gets ≥5 unit tests; estimated coverage ≥90% on the
  writer package. The reader's coverage stays at its current level.
- **`pytest-asyncio` accidentally introduced** (severity: LOW,
  inherited) — the project deliberately uses `asyncio.run` wrappers.
  **Mitigation:** the writer is sync (matches the reader's style);
  FastMCP wraps it. The shim is unchanged. Reject any
  `[tool.pytest.ini_options] asyncio_mode` change in `sdd-apply`.

### Open Questions / Decisions Needed

The orchestrator should bring these back to the user:

1. **HITL scope confirmation**: the writer is record-keeping, NOT
   destructive. Confirm with the user that NO HITL gate is required
   for `save_intervention_record`. (Issue #12 doesn't mention HITL;
   issue #15 does for `snmp_migrate_radio_frequency`. The blast-
   radius distinction is the key argument.)
2. **Filename template**: `INT-<ticket>-<ip>-<unix>-<hex>.json` per
   issue #12. Confirm the strict regex
   `[A-Za-z0-9_-]+` on `<ticket>` and `<ip>` components is
   acceptable (rejects `/`, `.`, whitespace). Confirm the 6-hex
   suffix is sufficient (vs. full UUID6).
3. **Sanitization on write vs read only**: defense-in-depth means the
   on-disk file is already masked. Confirm this is the desired
   contract — alternatives are (a) write verbatim and rely on read-
   side sanitization only, (b) sanitize on write AND read. The
   proposal recommends (b).
4. **Settings extension**: issue #12 doesn't ask for new settings. The
   design phase decides whether to add
   `nora_interventions_max_record_bytes` (default 64 KB) for the
   payload-size cap. Recommend YES for production safety.
5. **MCP wrapper location**: register the 5th `@mcp.tool` in
   `src/nora/server.py` (mirrors existing pattern) OR in
   `src/nora/intervention_writer/mcp_bridge.py` (self-contained
   package). Recommend `src/nora/server.py` for thin-server
   consistency.
6. **Coverage threshold**: writer package adds ~560 LOC. Confirm the
   85% threshold in `openspec/config.yaml` stays (estimated writer
   coverage ≥90%; reader coverage unchanged).
7. **Transport (stdio vs Streamable HTTP)**: NORA boots stdio today.
   Issue #12 mentions remote clients. Confirm the writer library
   function is the surface; transport wiring is a deployment
   concern (out of scope for this slice, same as Phase 3's MCP
   transport question).

### Non-Goals

Explicit list of what this slice does NOT cover:

1. **HITL for memory writes.** Out of scope. HITL lives in #15 for
   destructive RF changes. The writer is record-keeping; the blast-
   radius is recoverable by deleting the file.
2. **Reader writes.** The reader package stays read-only at 100%.
   The AST guard in `tests/intervention_memory/test_no_writes.py`
   is untouched. The writer package is a SIBLING, not a relaxation.
3. **Schema migration of v7 → v8 records.** Unchanged from Phase 3.
   The reader tolerates both shapes via `extra="ignore"`; the
   writer uses the same schema.
4. **Webui.db mirror shim for the writer.** Out of scope. If openchat
   wants a writer-side shim later, that's a new file in the writer
   package. Not requested by issue #12.
5. **Auto-write from #15 tools.** Out of scope for THIS change. The
   writer library function is reusable from library code (so #15
   CAN call it after `snmp_get_ap_summary` if it wants), but the
   #15 → #12 integration is a future change.
6. **Multi-tenant / multi-tenant-isolation.** NORA's writer writes
   to whatever is in `nora_interventions_dir`. Per-tenant separation
   is openchat's concern.
7. **Network I/O from NORA to remote `.22`.** NORA never reaches
   out. The writer writes local disk only.
8. **New runtime dependencies.** No new `pyproject.toml` runtime
   deps. `pydantic` is already a transitive of `pydantic-settings`;
   stdlib `json`, `os`, `secrets`, `pathlib`, `tempfile` cover the
   rest.
9. **MCP server split.** No separate FastMCP server for the writer.
   The 5th tool lives on NORA's existing `mcp` instance, mirroring
   the 4 read-only tools.
10. **Path-traversal guard for `intervention_id` regex only.** The
    guard does NOT cover a future `metadata` field the writer might
    accept; each new free-text field must add a sanitization rule
    (or be rejected outright). Document in the new spec.
11. **Quarantine of corrupt records.** The reader's
    `tolerant-storage` already skips corrupt files with a
    WARNING log line; no new quarantine mechanism is introduced.
    A future operational improvement (move corrupt files to
    `nora_interventions_dir/.corrupt/`) is out of scope.

### Ready for Proposal

**Yes — proceed to `sdd-propose`.** The issue is concrete enough
(named tool, schema, filename template, sanitization) that the
proposal phase can draft intent + scope without re-deriving them.
The orchestrator should:

1. Surface the **Open Questions** above to the user — especially
   #1 (HITL scope), #2 (filename regex strictness), and
   #3 (sanitization on write vs read).
2. Forecast ~560 LOC of new code (3 modules × ~80 LOC + 4 test files
   × ~80 LOC + the delta spec + 1 wrapper in `src/nora/server.py`).
   This fits the 400-line PR-review budget as **one PR if** the
   package + tests stay under 400, otherwise **two PRs** splitting
   on package (PR1: writer pure logic + tests) vs wiring
   (PR2: the 5th `@mcp.tool` + server edit + spec delta). `sdd-tasks`
   will decide; recommend the orchestrator ask the user up-front
   given the spec is otherwise clear.
3. Confirm **Approach 1 (new sibling package)** is the user's
   preferred architecture — Phase 3's R2 hard rule is the constraint
   that drove the recommendation; user acceptance of the
   "sibling, not relaxation" pattern is the foundation.