# Feature: Issue #70 — Post-sweep `SpectrumAnalysis.xml` HTTP download & RF bin decoding

**Status**: 🚧 Planned — implementation not started.
**Branch**: `feat/issue-70-spectrum-http-decode` (from `origin/main` @ `081ecb1`, NOT yet created).
**Issue**: [#70](https://github.com/alexandervazquez98/nora/issues/70) — `feat(spectrum): implement post-sweep SpectrumAnalysis.xml HTTP download & RF bin decoding` (issue #62 part 3).
**Depends on**: PR #66 (issue #62 part 1, merged) + PR #67 (issue #62 part 2, merged) + `SpectrumSweepResult` placeholders landed in WU-3.

---

## Why this doc exists

GitHub issue #70 reports that, after PR #66 landed the real Cambium PMP 450i spectrum sweep protocol (SET/GET-poll), the shipped helper `snmp_run_spectrum_analysis` returns `scan_outcome: "COMPLETED"` and `final_status: 4` correctly, BUT the typed `SpectrumSweepResult` always carries **empty** `ranked_clean_frequencies` and `noise_floor_dbm`. The placeholders were intentionally left as "future slice" in WU-3 (per the ODD doc for #62) so that this follow-up could land the bin decoding independently.

Physical-hardware evidence from the operator (Cambium PMP 450i firmware 25.0.1 / 25.1, 3.5 GHz, 1 AP + 14 SMs):
- After sweep completion (`.221.0 == 4`), the radio publishes `http://{ip}/SpectrumAnalysis.xml` on its web root.
- AP XML: ~42,879 bytes, 722 bins (361 frequencies × 2 polarizations V/H).
- SM XML: ~43,000 bytes each, 722 bins per radio.
- HTTP 200 OK on 15/15 devices (100% success rate).

The operator **cannot** recommend migration channels because the orchestrator receives empty arrays; this issue closes that gap.

---

## Operator decisions (resolved 2026-09-19)

| # | Decision | Rationale |
|---|---|---|
| 1 | **HTTP (not HTTPS) for `SpectrumAnalysis.xml`.** | Cambium radios expose the spectrum XML over plain HTTP on port 80; HTTPS is not supported on the radio's web root. The client must speak HTTP, defaulting to port 80, with the URL overridable for unusual deployments. |
| 2 | **Use `httpx` (synchronous) for the HTTP client.** | `httpx>=0.27` is already in `[dependency-groups].dev`; the rest of NORA is sync-first. Async is NOT introduced here — the helper's SNMP polling is already sync. The HTTP fetch is bounded (one GET per radio) so async concurrency gain is negligible for v1. Concurrency for SM fan-out is deferred to a later slice (issue #71 or follow-up). |
| 3 | **Sequenced fetch: AP first → wait SM reassociation → SMs in parallel (sequential for v1).** | The issue's three-step ladder is preserved in spirit, but the SM fan-out uses **sequential** fetching in WU-3 to keep the helper synchronous and the PR small. The "wait SM reassociation" step uses `Settings.nora_spectrum_sm_reassociation_timeout_seconds` (default 15s) bounded between the AP completion and the SM fetch start. Concurrency is tracked as a follow-up (does not block v1). |
| 4 | **Fixture is a small XML (~30-50 bins), sanitized, version-controlled.** | The full 722-bin XML is too large for hermetic tests and adds zero coverage value over a representative subset. The fixture exercises all four `<Freq>` element shapes (V/H polarizations × avg/max combinations). The fixture is checked into `tests/data/fixtures/spectrum/` so it ships with the test suite. |
| 5 | **`ranked_clean_frequencies` algorithm = "worst-case min first".** | For each unique frequency `f`: compute `worst_avg = max(avg_dbm across all 4 series: AP_V, AP_H, SM_V, SM_H)` (the channel's worst-leg noise). Sort frequencies ascending by `worst_avg`; return top `n` MHz values. `n` defaults to 10 (configurable via a new Settings knob). |
| 6 | **`noise_floor_dbm` keyed by `"{freq_mhz:.1f}"` (string), value = worst_avg_dbm (int).** | Matches the issue's wording "piso de ruido promedio/máximo por canal representativo". Per-channel aggregates are exposed in a sibling internal helper `noise_floor_detail()` returning `(worst_avg, worst_max, avg_per_leg)` for downstream callers that want richer diagnostics. |
| 7 | **New typed exceptions inherit `DriverError`.** | `SpectrumHttpFetchError` (HTTP non-2xx, connection refused, timeout, retry-exhausted) and `SpectrumXmlParseError` (malformed XML, missing `<Spectrum_Analyzer>`, malformed `<Freq>` attributes). Both inherit `DriverError` so `except DriverError` catches the entire surface — mirrors `SpectrumSweepTimeout`. |
| 8 | **Tier stays Tier-1; HTTP fetch is gated behind `operator_confirmed=True` like the SNMP sweep.** | The fetch emits a wire frame (HTTP GET) and reads sensitive RF telemetry; the existing Tier-1 gate at `fetch_spectrum` extends to cover the HTTP path. A separate Tier knob for HTTP-only is overkill for v1. |
| 9 | **Settings: 4 new knobs.** | `nora_spectrum_http_timeout_seconds` (default 10.0), `nora_spectrum_http_max_retries` (default 5), `nora_spectrum_http_retry_delay_seconds` (default 3.0), `nora_spectrum_sm_reassociation_timeout_seconds` (default 15.0), `nora_spectrum_ranking_top_n` (default 10). All knobs have model-validator bounds; the new `_validate_spectrum_http_settings` validator fails closed on misconfiguration. |

---

## Out of scope (this feature)

- **Async / concurrent SM fan-out.** Sequential SM fetch in v1. Tracked as a follow-up: issue / PR TBD.
- **Full 722-bin fixture.** A representative ~30-50 bin subset is shipped; the full fixture is the operator's responsibility (sanitized XML is too large to live in the repo).
- **Spectrum XML TLS / auth.** Cambium radios expose the XML without auth; if a future firmware adds basic auth, that's a separate slice.
- **Caching / dedup.** Each sweep fetches fresh; no on-disk cache, no deduplication across sweeps.
- **Schema migration of `SpectrumSweepResult`.** The two placeholder fields stay typed as `list[float]` / `dict[str, float]` so existing MCP consumers (which already `model_dump(mode="json")` the result) are compatible.

---

## Architectural decisions

| # | Decision | Rationale |
|---|---|---|
| 1 | **New module `src/nora/drivers/snmp_pmp450i/spectrum_http.py`** hosting the HTTP client, XML parser, and ranking helpers. | Cleanly separates network I/O + XML parsing from the existing `spectrum.py` (which is SNMP-only). The new module re-exports the public symbols (`SpectrumBin`, `fetch_spectrum_xml`, `parse_spectrum_xml`, `rank_clean_frequencies`, `noise_floor_per_channel`) so callers do not need to learn a second import path. |
| 2 | **New typed model `SpectrumBin(BaseModel)` with `frozen=True`.** | Mirrors `SpectrumSweepResult` style; `frequency_mhz: float`, `polarization: Literal["V", "H"]`, `avg_dbm: int`, `max_dbm: int`. |
| 3 | **No new HTTP client class; pure functions with an injected `httpx.Client`.** | Easier to test (inject a fake `httpx.Client` that returns canned responses). Mirrors the existing test seam for SNMP (`_FakeWritableSnmpClient`). |
| 4 | **Parser uses `xml.etree.ElementTree` (stdlib).** | No new runtime dep. The XML is small (~40 KB) and flat; stdlib is fast enough. `lxml` would add a dep for no benefit at this scale. |
| 5 | **`fetch_spectrum_xml(host)` returns raw XML text; parser is separate.** | Lets tests bypass the HTTP layer for parser-only tests (and vice versa). Mirrors the existing split between `fetch_spectrum` (SNMP) and `parse_*` (not yet extracted). |
| 6 | **Retry loop with exponential backoff capped by `nora_spectrum_http_max_retries`.** | Cambium radios are slow to start the web server after a sweep (operator observation: ~5-15s of HTTP silence while the radio reloads). 5 retries × 3s delay = 15s of patience before failing; matches the issue's "5 reintentos con 3s de delay". |
| 7 | **Zero-Leakage**: `device.host` is already a TEST-NET-1 / RFC 5737 IPv4 literal at the inventory layer; the URL builder does not embed any other identifier. | The host string flows through unchanged; no new sanitization is needed at this layer (the existing `Sanitizer` handles it at the MCP boundary). |

---

## Work units

Each WU closes with one work-unit commit on the feature branch. WUs run sequentially because each unblocks tests for the next.

### WU-1: HTTP client + XML parser + models + fixture + exceptions (no integration yet)

**Touch**:
- `src/nora/drivers/exceptions.py` — add `SpectrumHttpFetchError` and `SpectrumXmlParseError` (both inherit `DriverError`); export from `__all__`
- `src/nora/drivers/snmp_pmp450i/spectrum_http.py` — NEW module hosting:
  - `class SpectrumBin(BaseModel)` with `frozen=True` (`frequency_mhz`, `polarization: Literal["V", "H"]`, `avg_dbm`, `max_dbm`)
  - `fetch_spectrum_xml(host: str, *, http_client: httpx.Client | None = None, timeout_seconds: float = 10.0, max_retries: int = 5, retry_delay_seconds: float = 3.0) -> str` — GETs `http://{host}/SpectrumAnalysis.xml` with retry loop
  - `parse_spectrum_xml(xml_text: str) -> list[SpectrumBin]` — parses `<Spectrum_Analyzer>` + `<Freq f="..." avg="..." max="..." />` elements
  - `build_spectrum_url(host: str, *, scheme: str = "http", port: int = 80, path: str = "/SpectrumAnalysis.xml") -> str` — URL builder (overridable for unusual deployments)
- `src/nora/config.py` — 5 new knobs + `_validate_spectrum_http_settings` validator:
  - `nora_spectrum_http_timeout_seconds: float = 10.0` (bounds [1.0, 60.0])
  - `nora_spectrum_http_max_retries: int = 5` (bounds [0, 20])
  - `nora_spectrum_http_retry_delay_seconds: float = 3.0` (bounds [0.1, 30.0])
  - `nora_spectrum_sm_reassociation_timeout_seconds: float = 15.0` (bounds [1.0, 60.0])
  - `nora_spectrum_ranking_top_n: int = 10` (bounds [1, 100])
- `tests/data/fixtures/spectrum/pmp450i_spectrum_sample.xml` — NEW sanitized fixture (~30-50 bins; covers all 4 `<Freq>` shapes; references the issue's operator-observed values from the production sector: 3500.0 MHz / 3550.0 MHz / 3560.0 MHz / 3650.0 MHz with V/H avg/max)
- `tests/test_snmp_spectrum_http.py` — NEW test file (~15 hermetic tests):
  - `SpectrumBin` model: frozen, JSON-serialisable, validation rejects unknown polarization
  - `build_spectrum_url`: default (HTTP/80/`/SpectrumAnalysis.xml`), custom scheme/port/path
  - `fetch_spectrum_xml`:
    - Happy path: 1 GET → 200 OK → body returned (uses `httpx.MockTransport` so no real network)
    - Retry loop: 4 × `ConnectError` then 200 OK → body returned
    - Retry exhausted: 5 × `ConnectError` → raises `SpectrumHttpFetchError`
    - HTTP non-2xx: 404 → raises `SpectrumHttpFetchError`
    - Timeout: `httpx.TimeoutException` → raises `SpectrumHttpFetchError`
    - `max_retries=0`: 1 attempt, no retry
  - `parse_spectrum_xml`:
    - Happy path: 30 bins from the fixture → 30 `SpectrumBin` records
    - Mixed V/H polarizations: 15 V + 15 H
    - Empty `<Spectrum_Analyzer>` → empty list (not an error)
    - Malformed XML (random bytes) → raises `SpectrumXmlParseError`
    - Missing `<Freq>` `avg` attribute → raises `SpectrumXmlParseError`
    - Missing `<Spectrum_Analyzer>` root → raises `SpectrumXmlParseError`
  - `SpectrumHttpFetchError` / `SpectrumXmlParseError` inherit `DriverError`
- `tests/test_config.py` — extend field-count assertion if it enumerates Settings fields

**Change**: All new files; no existing module behavior changes. The new module is a leaf that can be imported without side effects.

**Tests**: `pytest tests/test_snmp_spectrum_http.py tests/test_config.py` must pass. `ruff check src/nora tests` + `mypy src/nora` must pass.

### WU-2: Integrate into `spectrum.py` + ranking + populate

**Touch**:
- `src/nora/drivers/snmp_pmp450i/spectrum.py`:
  - Import the new helpers from `spectrum_http`
  - Add `RankingDetail` helper return type (or reuse `list[SpectrumBin]` directly)
  - Extend `fetch_spectrum(...)` to perform the post-sweep HTTP fetch ladder:
    1. After `final_status in _SWEEP_COMPLETION_STATUSES`, fetch the AP XML (call `fetch_spectrum_xml(host)`)
    2. `time.sleep(nora_spectrum_sm_reassociation_timeout_seconds)` (bounded wait for SM reassociation)
    3. Fetch each SM XML (sequential for v1 — concurrent is a follow-up)
    4. Parse all XML payloads via `parse_spectrum_xml`
    5. Compute `noise_floor_dbm = noise_floor_per_channel(all_bins)`
    6. Compute `ranked_clean_frequencies = rank_clean_frequencies(all_bins, top_n=nora_spectrum_ranking_top_n)`
    7. Populate the `SpectrumSweepResult` with the computed values
  - **Gating**: the new fetch ladder runs ONLY when `operator_confirmed=True` AND `final_status in {3, 4}` (the sentinels that imply results are available). Sentinel `0` (defensive abort) skips the fetch; the result still reports COMPLETED with empty arrays and a log line.
- `src/nora/server.py` — extend the docstring of `snmp_run_spectrum_analysis` to document the populated `ranked_clean_frequencies` and `noise_floor_dbm` fields. No signature change; the new fields are already in the return type.

**Change**: behavior change in `fetch_spectrum` — the helper now waits ~15s after sweep completion before returning (sequential SM fetch adds another ~5-15s). The MCP tool's Tier-1 gate stays unchanged.

**Tests**: extend `tests/test_snmp_spectrum.py`:
- Add a `_FakeHttpClient` fixture that mimics `httpx.Client` (records requests; serves canned responses)
- Happy path: sweep completes (final_status=4) → HTTP fetch runs → `noise_floor_dbm` + `ranked_clean_frequencies` populated
- Sentinel `0`: sweep completes → HTTP fetch is SKIPPED → fields stay empty
- HTTP fetch fails: sweep completes → HTTP raises `SpectrumHttpFetchError` → `SpectrumSweepResult` carries the error in a new `post_sweep_error: str | None = ""` field (set; non-blocking; the operator sees the sweep succeeded but the bin decode failed)
- Sentinel `3` (no results): sweep completes → HTTP runs → XML is `<Spectrum_Analyzer></Spectrum_Analyzer>` empty → fields stay empty, no error

**New model field**: `SpectrumSweepResult.post_sweep_error: str = ""` (default empty; populated with the error message when the HTTP/parse step fails non-fatally; carries Zero-Leakage contract via the existing sanitiser).

### WU-3: Tool wiring + docs + verification

**Touch**:
- `docs/tool_specs/snmp_run_spectrum_analysis.md` — NEW spec file documenting the populated fields and the HTTP fetch ladder. Mirrors `icmp_list_probe_runs.md` style (front-matter + section structure).
- `OPERATIONS.md` — note the new behavior: post-sweep HTTP fetch runs after every successful sweep; `Settings.nora_spectrum_*` knobs are listed in the config section.
- `INSTALL.md` — only if there's a config-knob listing that needs cross-linking.
- `.env.example` — list the 5 new `nora_spectrum_http_*` / `nora_spectrum_sm_reassociation_timeout_seconds` / `nora_spectrum_ranking_top_n` knobs with their default values.

**Change**: documentation only; no behavior change.

**Tests**: `pytest tests/ -q` (full suite) must pass. `ruff check src/nora tests` + `mypy src/nora` must pass.

### WU-4: End-to-end verification

**Touch**: none — verification only.

**Steps**:
- `pytest tests/test_snmp_spectrum.py tests/test_snmp_spectrum_http.py tests/test_config.py tests/test_oid_catalog_integration.py` — must pass.
- `ruff check src/nora tests` and `mypy src/nora` — must pass.
- `pytest tests/ -q` (full suite) — must pass.
- Manual smoke: `python -c "from nora.config import Settings; s = Settings(_env_file=None, _env_file_encoding=None); print(s.nora_spectrum_http_timeout_seconds, s.nora_spectrum_ranking_top_n)"` — must show defaults.

---

## Risk register

| Risk | Mitigation |
|---|---|
| `httpx` import at runtime adds a dependency to the production install. | `httpx>=0.27` is already in `[dependency-groups].dev`; promote it to `[project].dependencies` in `pyproject.toml` in WU-1. The HTTP client is only invoked when `operator_confirmed=True`, so it is not on the read-only path. |
| Cambium radio's web server is slow to start after a sweep; HTTP fetch might race. | 5 retries × 3s delay (default) gives 15s of patience; configurable via `nora_spectrum_http_max_retries` and `nora_spectrum_http_retry_delay_seconds`. |
| 722-bin XML is too large to fixture; tests don't cover the full shape. | Fixture is a representative ~30-50 bin subset. The parser is `O(n)` over `<Freq>` elements; the only behavioural difference vs 722 bins is timing, which is irrelevant to the parser tests. |
| Sequential SM fetch in v1 is slow (~14 SMs × 3s timeout = 42s worst case). | Logged as a follow-up. v1 keeps the PR small; the issue operator explicitly accepted sequential in the in-conversation decision (2026-09-19). |
| `SpectrumSweepResult` schema gains a field (`post_sweep_error`); MCP consumers might break. | The new field has a default value (`""`); existing `model_dump(mode="json")` consumers receive the same fields plus one new key. No breaking change. |
| Two unrelated spectrum slices (#62 + #70) on different branches → confusing PR review. | The ODD doc keeps WU-1..4 self-contained; the PR description mirrors the four-bullet structure. WU-1 is reviewable in isolation (pure additions, no behavior change). |

---

## Related work (non-blocking)

- `odd/tasks/issue-62-spectrum-and-multi-community.md` (closed via PRs #66 + #67) — the SMP sweep protocol + multi-community overrides this issue builds on.
- Issue #69 — SM table siteName/ipAddress (independent; not coupled to #70).
- Open PRs #63, #64, #65 — issue #61 (ICMP stability probe); independent.

## Status log

- **2026-09-19 — Plan scaffolded.** ODD doc created; awaiting user authorization to start WU-1.
