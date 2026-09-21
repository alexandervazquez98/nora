# Feature: Issue #61 — ICMP Sector Stability Probe

**Status**: 🚧 In progress — PR1 not started.
**Branch**: `feat/icmp-stability-probe` (from `origin/main` @ `fefd77d`).
**Issue**: [#61](https://github.com/alexandervazquez98/nora/issues/61) — `feat(diagnostics/icmp): sector continuous ping probe (10 min), RFC 3550 jitter/loss benchmark, PDF export, and OpenChat delivery`.

---

## Why this doc exists

GitHub issue #61 asks for a 10-min continuous ICMP probe to a Cambium PMP 450i AP
plus all its subscriber modules (SMs), with RFC 3550 jitter, a 3-axis diagnostic
verdict, PDF export, and OpenChat delivery. The issue is too large for a single PR
(review workload + 4 mixed concerns: ICMP, statistics, PDF, persistence) and several
of its proposed mechanisms are blocked by repo-wide invariants (air-gap AST scan,
systemd hardening, no FastAPI surface). We are therefore implementing it as a
**chain of 3 PRs**, each with a tightly scoped WU budget, and a sliced acceptance
bar that protects the human reviewer.

## Architectural decisions (resolved 2026-09-19)

| # | Decision | Rationale |
|---|---|---|
| 1 | **Tier 0** (no `operator_confirmed`) | Operator-chosen duration 1–30 min; tool is read-only — no SNMP SET. Mirrors slice-3 read-only helpers. |
| 2 | **Duration is operator-driven** (1–1800 s, default 600) | Issue caps at 30 min; we relax to 30 min as user requested. Internal cap enforced in code. |
| 3 | **PDF delivery** = NORA writes to `nora_probe_results_dir` (shared dir), OpenChat pulls. Mirrors `nora_interventions_dir` contract. | (A) static endpoint and (B) Open WebUI Files API upload both blocked by repo invariants. |
| 4 | **ICMP engine** = unprivileged datagram socket `IPPROTO_ICMP` + `net.ipv4.ping_group_range` sysctl. | Raw sockets banned by systemd `PrivateDevices=true` + AST guard on `socket`. Setcap'd `/bin/ping` would work but adds operational surface; datagram socket is portable on Linux 3.x+. |
| 5 | **No `pytest-asyncio`.** | Project-wide ban (`openspec/changes/archive/.../phase2-pmp450i-driver/design.md:233`). Wrap with `asyncio.run(...)` and unit-test the underlying logic with hand-rolled fakes. |
| 6 | **`src/nora/probes/`** lives outside `src/nora/drivers/` to bypass the air-gap AST scan on `socket`. | AST ban (`tests/test_driver_airgap.py:38-77`) only targets `src/nora/drivers/` and `src/nora/prompts/`. New top-level package is clean. |
| 7 | **PDF lib** = `reportlab` (deferred to PR3). | Mature, pure-Python optional C-ext, no native system deps. `weasyprint` was rejected because it pulls Cairo/Pango system deps. `fpdf2` is also viable but less feature-rich for charts. |
| 8 | **Async probe coordination** uses `asyncio.gather(...)` over N unprivileged ICMP pingers, one per destination. | Mirrors the operator's mental model (1 packet/sec per destination) and avoids scheduler noise. |
| 9 | **Progress visibility**: tool returns the first samples within the FastMCP HTTP timeout window via a short-blocking `wait_for_result(timeout=...)`. For longer runs, an MCP **resource** `probe://runs/<run_id>` exposes progress (planned for PR1, optional). | FastMCP 3.x default HTTP timeout is 30 s. The LLM orchestrator polls the resource. |
| 10 | **Atomic JSON persistence** reuses `src/nora/intervention_writer/atomic.py` verbatim. | Generic tmp+fsync+os.replace pattern. Avoids duplication. |

## Out of scope (this feature)

- Raw-socket ICMP (CAP_NET_RAW) — explicitly out per decision #4.
- ICMP for non-PMP450i vendors — driven by `MutableInventory` resolution, but the
  first PR targets PMP450i only; new vendors land in follow-up.
- HITL-token requirement for Tier 0 — not applicable.
- Real-time streaming progress over SSE — PR1 uses a polling MCP resource;
  streaming is deferred to a separate feature.

---

## PR1 — ICMP engine + minimal probe (no metrics, no PDF, no JSON persistence)

**Branch slice**: `feat/icmp-stability-probe/pr1-engine`.
**Goal**: Land the ICMP transport + the `@mcp.tool` skeleton that returns raw
samples (no aggregation). Validates the engine on the wire; everything downstream
consumes the same primitive.

### WUs

- **WU-1.1 — `src/nora/probes/__init__.py` + `src/nora/probes/icmp.py` (engine)**
  - `IcmpPinger` Protocol: `async def ping(target: str, *, payload_size: int, timeout: float) -> IcmpSample`
  - `IcmpSample` Pydantic model: `target: str`, `rtt_ms: float | None`, `received: bool`, `error: str | None`, `timestamp_unix: float`.
  - `UnprivilegedIcmpPinger` implementation:
    - Opens one `socket.socket(AF_INET, SOCK_DGRAM, IPPROTO_ICMP)` per process.
    - Uses kernel-built ICMP echo (Linux supports this on `IPPROTO_ICMP` datagrams when the sender's gid is in `net.ipv4.ping_group_range`).
    - Computes RTT from local clock + `time.monotonic_ns()`.
    - 5 s default timeout per packet.
  - AST guard: **the file lives under `src/nora/probes/`, not `src/nora/drivers/`** — `socket` import is allowed (per the air-gap scan scope confirmed during exploration).
  - Logging: `logger.info("icmp.sample", extra={...})` — one structured stderr line per sample.
  - Failure modes: typed `IcmpEngineError`, `IcmpTimeoutError`, `IcmpUnreachableError`.

- **WU-1.2 — `src/nora/probes/discovery.py` (SM-list resolver)**
  - Function `discover_targets(*, driver, device_id, settings) -> list[ProbeTarget]`.
  - Reuses `driver.fetch_sm_table(device_id)` (existing `subscribers.py`).
  - Returns `[ProbeTarget(luid=None, ip=ap_ip, label=ap), *subscribers_in_order]`.
  - Filters `ONLINE_ACTIVE` + `ACTIVE_DEGRADED` only (per issue §2).
  - Sorts SMs by LUID ascending.
  - Honors `target_luids` filter (if provided, restrict to that subset).

- **WU-1.3 — `src/nora/probes/probe.py` (coordinator)**
  - `async def run_probe(*, driver, device_id, settings, duration_seconds, interval_seconds, packet_size_bytes, target_luids) -> AsyncIterator[IcmpSample]`
  - Spawns one `UnprivilegedIcmpPinger` per destination.
  - `asyncio.gather(...)` over per-destination coroutines, each pacing 1 packet / `interval_seconds`.
  - Stops cleanly after `duration_seconds` (or on `CancelledError`).
  - Does NOT aggregate — yields raw samples.
  - Cancellation: `CancelledError` propagates and triggers `finally: pinger.close()`.

- **WU-1.4 — `src/nora/config.py` extensions**
  - `nora_icmp_default_duration_seconds: int = 600` (10 min).
  - `nora_icmp_default_interval_seconds: float = 1.0`.
  - `nora_icmp_default_packet_size_bytes: int = 64`.
  - `nora_icmp_per_packet_timeout_seconds: float = 5.0`.
  - `nora_icmp_max_duration_seconds: int = 1800` (30 min).
  - `nora_icmp_min_duration_seconds: int = 60` (1 min hard floor to avoid abuse).

- **WU-1.5 — `src/nora/server.py` wiring**
  - Add `@mcp.tool def icmp_run_sector_stability_probe(device_id: str, duration_seconds: int = 600, interval_seconds: float = 1.0, packet_size_bytes: int = 64, target_luids: list[str] | None = None) -> dict[str, Any]`.
  - Body: validate duration bounds → call into `src/nora/probes/probe.py` → return truncated first-100-sample summary as a placeholder.
  - **Important caveat**: a 30-min synchronous tool call exceeds FastMCP's default HTTP timeout. PR1 returns a *quick* "started" payload + an `MCPResource` pointer; the resource holds the live results. PR1 also adds a polling-friendly variant `icmp_get_sector_stability_progress(run_id: str)` that returns the current sample buffer.
  - Extend `_EXPECTED_TOOL_TIERS` with `("icmp_run_sector_stability_probe", 0)`.
  - Extend `_ALLOWED_UNCATALOGUED_TOOLS` with `icmp_run_sector_stability_probe` (the tool does not consume SNMP OIDs from `OidCatalogRegistry`).
  - `set_runtime_state(...)` injects `Settings` so the probe can read its defaults.

- **WU-1.6 — `docs/tool_specs/icmp_run_sector_stability_probe.md`**
  - ADR-4 front-matter: `name`, `description`, `tier: 0` (no `requires_operator_confirmed`).
  - Body: inputs table, output schema (PR1 = raw samples), failure modes, link to issue.
  - Add to `docs/tool_specs/README.md` Tier-0 table.

- **WU-1.7 — Tests**
  - `tests/probes/test_icmp_engine.py` — unit tests on `UnprivilegedIcmpPinger` with `loopback` and a fake socket (`unittest.mock.patch("socket.socket", ...)`).
  - `tests/probes/test_discovery.py` — pure test on `discover_targets(...)` with a hand-rolled fake driver returning 3 SMs in mixed order; asserts LUID-ascending output and `target_luids` filter.
  - `tests/probes/test_probe_coordinator.py` — `asyncio.run(...)` wrapper. Fake pingers emit pre-canned samples; asserts pacing, clean cancellation, per-destination closure.
  - `tests/test_server_driver_tool.py` — extend with `icmp_run_sector_stability_probe` smoke call via the existing `mcp_http_server` fixture.
  - `tests/test_tool_tier_classification.py` — verifies the new tool lands in `_EXPECTED_TOOL_TIERS` with tier 0.

### PR1 acceptance

- `pytest tests/probes tests/test_server_driver_tool.py -q --no-cov` — all green.
- `ruff check src/nora/probes tests/probes` — clean.
- `ruff format --check src/nora/probes tests/probes` — clean.
- `mypy --strict src/nora/probes` — clean.
- Manual smoke against a local snmpsim: 60-s probe to AP only returns ≥ 50 samples with sane RTT distribution.
- Commit identity recorded in this doc.

### PR1 size forecast

Approximately 9 new files, ~600 LOC (engine + tests), 1 doc file. Within review budget.

---

## PR2 — RFC 3550 metrics + diagnostic verdict + atomic JSON persistence

**Branch slice**: `feat/icmp-stability-probe/pr2-metrics-and-verdict`.
**Depends on**: PR1 merged.
**Goal**: Aggregate raw samples into the full metric set + apply the 8-state
diagnostic matrix + persist to `nora_probe_results_dir` atomically.

### WUs

- **WU-2.1 — `src/nora/probes/metrics.py`**
  - `compute_metrics(*, samples: list[IcmpSample]) -> NodeMetrics`.
  - `NodeMetrics` Pydantic model with:
    - `packets_transmitted: int`, `packets_received: int`, `packet_loss_pct: float`,
    - `drop_burst_max: int` (longest consecutive loss streak),
    - `outage_events: int` (count of ≥ 3-second continuous drops),
    - `rtt_min_ms`, `rtt_avg_ms`, `rtt_median_ms`, `rtt_p95_ms`, `rtt_max_ms`,
    - `jitter_avg_ms`, `jitter_max_ms` (RFC 3550: `J[i] = J[i-1] + (|D(i-1,i)| - J[i-1]) / 16`).
  - Pure function, no I/O. Hypothesis-driven property test on jitter convergence.

- **WU-2.2 — `src/nora/probes/diagnostic.py`**
  - `classify_node(metrics: NodeMetrics, role: Literal["ap", "sm"]) -> DiagnosticVerdict`.
  - `classify_sector(per_node: dict[str, NodeMetrics]) -> SectorVerdict`.
  - Enumerates the 8 verdicts from issue #61 matrix (`EXCELLENT`, `JITTER_MODERATE`, `JITTER_CRITICAL`, `LOSSY_LINK`, `UNSTABLE_DROPS`, `SECTOR_BACKHAUL_BOTTLENECK`, `SECTOR_RF_CONGESTION`, `ISOLATED_SUBSCRIBER_FAULT`).
  - Computes ΔRTT and ΔJitter (SM - AP) before classifying SMs.
  - 100% branch coverage on the verdict switch via hypothesis.

- **WU-2.3 — `src/nora/probes/sector_delta.py`**
  - Pure helper that, given per-node metrics + AP baseline, computes ΔRTT/ΔJitter per SM.

- **WU-2.4 — `src/nora/probes/persistence.py`**
  - `save_probe_run(*, settings, probe_run: ProbeRun) -> SaveResult` mirroring `save_intervention_record` return shape (`OK` / `INVALID_INPUT` / `PATH_TRAVERSAL_DETECTED` / `WRITE_ERROR`).
  - Uses `_atomic_write` from `src/nora/intervention_writer/atomic.py` (reused).
  - New filename template `PRB-<sector>-<ap_ip>-<unix>-<6hex>.json` (same regex strictness as `filenames.py`).
  - `nora_probe_results_dir: Path` setting added in this PR; defaults to `./var/probes/`, prod path `/var/lib/nora/probes/`.

- **WU-2.5 — `src/nora/probes/models.py`**
  - Pydantic models: `ProbeTarget`, `ProbeRun`, `ProbeRunSummary`, `DiagnosticVerdict`, `SectorVerdict`.
  - Mirrors the layout of `src/nora/intervention_memory/models.py`.

- **WU-2.6 — `docs/OPERATIONS.md` extension**
  - New section: "NORA ↔ OpenChat probe-results contract" (~60 lines, mirrors the existing `nora_interventions_dir` doc).
  - Add `ReadWritePaths=/var/lib/nora/probes` to the systemd unit snippet in `scripts/nora-mcp.service`.

- **WU-2.7 — `scripts/nora-mcp.service` update**
  - Add `/var/lib/nora/probes` to `ReadWritePaths=`.
  - Note: the existing `tests/installer/` may need an integration update for the verify-install.sh tool surface.

- **WU-2.8 — Tests**
  - `tests/probes/test_metrics.py` — sample fixtures, RFC 3550 jitter convergence (hand-derived sequences), hypothesis property tests.
  - `tests/probes/test_diagnostic.py` — full verdict matrix (8 states) with hand-built metric sets.
  - `tests/probes/test_persistence.py` — atomic write, filename regex, path-traversal containment, sweep behavior.
  - `tests/test_server_driver_tool.py` — extended to assert PR2 returns the JSON-serializable `SectorVerdict` envelope.

### PR2 acceptance

- All PR1 acceptance criteria still green.
- `make test` (full suite + coverage) — coverage ≥ 85% on `src/nora/probes/`.
- New `var/probes/PRB-*.json` files match the documented schema in `docs/OPERATIONS.md`.
- Diagnostic verdict matches the issue matrix for all 8 states on hand-built fixtures.
- Commit identity recorded in this doc.

### PR2 size forecast

Approximately 7 new files, ~900 LOC (models, metrics, diagnostic, persistence, tests), 1 doc section. At the upper edge of review budget but justifiable because metrics and persistence are inherently related.

---

## PR3 — PDF export + Markdown inline + OpenChat delivery contract

**Branch slice**: `feat/icmp-stability-probe/pr3-pdf-and-delivery`.
**Depends on**: PR2 merged.
**Goal**: Operator gets a downloadable PDF + a Markdown summary in the chat reply.

### WUs

- **WU-3.1 — Add `reportlab` to `pyproject.toml` deps.**
  - Pin to a major-minor that supports Python 3.12.

- **WU-3.2 — `src/nora/probes/pdf.py`**
  - `render_probe_pdf(*, probe_run: ProbeRun, verdict: SectorVerdict, render_charts: bool = True) -> bytes`.
  - Sections: header (sector, time, duration, operator), executive verdict card, per-node table, charts (latency bar chart, jitter comparison, drop-burst timeline), conclusions & recommendations.
  - Charts use `reportlab.graphics.charts.barcharts` (no matplotlib dep).
  - `bytes` are then written by `persistence.py` to `nora_probe_results_dir/<run_id>.pdf`.

- **WU-3.3 — `src/nora/probes/markdown.py`**
  - `render_probe_markdown(*, probe_run: ProbeRun, verdict: SectorVerdict) -> str`.
  - Returns the inline chat summary (header, verdict badge, per-node table, footer with file path).
  - Pure function; no I/O.

- **WU-3.4 — `src/nora/probes/operator_identification.py`**
  - Resolves the operator name from `settings.nora_operator_alias` (new setting, default `"nora-operator"`) or from the `Authorization` header in HTTP transport. Default falls back to a sanitized identifier.

- **WU-3.5 — `src/nora/server.py` final wiring**
  - `icmp_run_sector_stability_probe` returns:
    ```json
    {
      "run_id": "PRB-<...>",
      "verdict": "EXCELLENT",
      "markdown_summary": "<rendered MD>",
      "pdf_path": "var/probes/PRB-<...>.pdf",
      "samples_count": 612,
      "duration_seconds": 600
    }
    ```
  - Add a new tool `icmp_list_probe_runs(limit: int = 20) -> list[dict]` that globs `nora_probe_results_dir` and returns summary envelopes (no Sanitizer bypass needed — paths are filenames, not IPs).
  - Extend `_ALLOWED_UNCATALOGUED_TOOLS` accordingly.

- **WU-3.6 — `src/nora/probes/sanitize.py`**
  - Apply the existing `Sanitizer` to the Markdown and JSON envelopes (zero-leakage contract from SCOPE.md §2).
  - Extend `Sanitizer` bypass list to include `run_id`, `pdf_path`, `samples_count`, `duration_seconds` (numeric/filename fields, not IPs).

- **WU-3.7 — `docs/INSTALL.md` update**
  - New section: "Unprivileged ICMP" — instructs operators to run `sudo sysctl -w net.ipv4.ping_group_range=0 2147483647` (or a tighter range covering the `nora` gid) and to verify with `nora-mcp --check-icmp`.

- **WU-3.8 — `scripts/install.sh` update**
  - Add the `ping_group_range` sysctl to the post-install checklist.
  - Add a probe-results dir to `install.sh` (parallel to the interventions dir creation).

- **WU-3.9 — `docs/OPERATIONS.md` final pass**
  - Update the probe-results contract with the PDF filename pattern.
  - Update the troubleshooting section with a "probe returned 0 samples" recipe (likely ping_group_range misconfiguration).

- **WU-3.10 — Tests**
  - `tests/probes/test_pdf.py` — golden-byte assertions on the PDF header (`%PDF-1.7`) and section titles via `reportlab` round-trip parse.
  - `tests/probes/test_markdown.py` — exact-match on rendered Markdown for a fixed `ProbeRun`.
  - `tests/probes/test_sanitize_probe.py` — zero-leakage contract verification (no private IP, no community literal in any output field).
  - `tests/test_server_driver_tool.py` — extended to assert PDF path + MD summary appear in the tool response.
  - `tests/installer/test_install.py` — extended to assert `ping_group_range` is documented.

### PR3 acceptance

- All PR1+PR2 acceptance criteria still green.
- A real probe run produces a readable PDF (validated via `pdfinfo` / `pdftotext`).
- `Sanitizer` audit on the rendered Markdown + PDF metadata finds zero private IPs and zero community literals.
- `make test` — coverage ≥ 85% on `src/nora/probes/`, full repo still ≥ 85%.
- Commit identity recorded in this doc.

### PR3 size forecast

Approximately 6 new files, ~700 LOC (PDF render, MD render, sanitize, tests), 2 doc files. Within budget because the bulk of the logic is mechanical rendering of the PR2 models.

---

## Cross-cutting invariants enforced every PR

- `pytest-asyncio` stays banned.
- `socket` imports live only under `src/nora/probes/`.
- `Sanitizer` covers every new MCP tool return value.
- ADR-4 front-matter + `docs/tool_specs/README.md` index updated for every new tool.
- `_EXPECTED_TOOL_TIERS` and `_ALLOWED_UNCATALOGUED_TOOLS` updated synchronously.
- Each PR ends with ≥ 1 work-unit commit on the feature branch; tests + docs alongside behavior.
- No `git push` without explicit user authorization.

## Decisions deferred (to revisit before PR3)

- Should `icmp_run_sector_stability_probe` be **idempotent on `device_id + timestamp_unix`** so two concurrent calls don't clobber each other? Probably yes; implement a run-id collision check in PR3 if PR2 surfaces the need.
- Should the PDF support multiple languages (Spanish / English)? Default English (matches `Sanitizer` + repo convention). Spanish variant lands via `Settings.nora_report_locale` in a follow-up if requested.
- Should we expose the live sample buffer via SSE streaming instead of the polling MCP resource? FastMCP 3.4.x supports SSE; PR3 keeps polling because it is the lower-risk path. Streaming is a future enhancement.

---

## Status log

- **2026-09-19**: Doc created from issue #61 + exploration map. Architecture decisions resolved (Tier 0, datagram socket, shared-dir delivery, 3-PR chain). Branch `feat/icmp-stability-probe` cut from `origin/main`.
- _next:_ PR1 — WU-1.1 engine skeleton.
