# Issue #97 — Pre-flight probes stale inventory host instead of live AP-reported IP

**Branch:** `fix/issue-97-preflight-live-host` (from `origin/main` @ `15b426b`)
**Issue:** https://github.com/alexandervazquez98/nora/issues/97
**Status:** Shipped — PR #99 merged (`aff9cd2`), released as v0.3.11

## Objective

Make `nora_snmp_migrate_radio_frequency`'s pre-flight probe the **live** SM address
the AP reported, not the stale one held in `devices.yaml`. Also translate the
remaining untyped wire failures in `register_device` so the operator gets an
actionable error instead of a bare OID literal.

## Problem

A production operator asked to migrate an AP carrying 10–15 SMs. Pre-flight aborted
with `DeviceUnreachable` against two SMs whose `devices.yaml` entries pointed at
retired IPs. The AP's live SM-table reported the current IPs; the pre-flight never
used them.

**Root cause** — `src/nora/drivers/snmp_pmp450i/migrate.py`:

1. `migrate.py:370-376` — the live `sm_ip` from the AP's SM-table walk is threaded
   into `_resolve_sm_device`.
2. `_resolve_sm_device` (`migrate.py:641-647`) — tier 1 (by IP) misses when
   inventory lacks that IP; tier 2 (`inventory.get(luid)`) hits, returning an entry
   with a stale `host`.
3. `migrate.py:433` — `host = str(getattr(sm_device, "host", None) or "")` reads the
   **stale** host off that entry.
4. `migrate.py:471` — `effective_device = sm_device` sends the stale-host device
   on the wire.

The live `sm_ip` reaches the resolver and is discarded at the `else` branch.

**Diagnostic tell that misdirected the initial triage:** `community_source='INVENTORY'`
is not evidence that the host came from inventory. It is evidence that *no
`sm_communities` override was supplied* (`_resolve_sm_community`, `migrate.py:709-712`)
— which is exactly the condition under which the stale-host path is taken.

## Scope (3 parts, as approved in issue body v4)

| Part | Change | File |
| --- | --- | --- |
| 1 | Pre-flight probe targets the AP-reported live IP | `migrate.py` |
| 2 | Prompt-conformance step for the AP-side ad-hoc path | `netops_orchestrator.md` |
| 3 | `register_device` translates remaining wire failures to typed errors | `register_device.py` |

**Not in scope (explicitly rejected):**
- SM-side abort gate (`migrate.py:955-956`) — four tests pin it; HITL-token-economy
  argument in `CommunityValidationFailed`'s docstring stands.
- A `soft_warnings` field — would duplicate the existing
  `nora_preflight_community_validation` kill switch (`config.py:100`).
- A `community` parameter on `snmp_migrate_radio_frequency` — new credential-bearing
  wire vocabulary on a Tier-2 mutation tool under Zero-Leakage.
- Fixing the tier-3 substring collision itself — the override makes it fail safe.

## Constraints

- Zero new tool parameters, state, config flags, or tool verbs.
- The four pinning tests stay green:
  `test_preflight_raises_when_one_sm_unreachable`,
  `test_preflight_raises_when_one_sm_wrong_community`,
  `test_preflight_raises_when_sm_not_in_inventory`,
  `test_preflight_does_not_consume_hitl_token_on_failure`.
- Part 3 catches `DriverError`, **not** bare `Exception` — a bare catch would also
  swallow `DuplicateDeviceError` (`mutable_inventory.py:107`), a real actionable
  condition documented in the tool contract.

## Tasks

- [x] **T1** — RED: `test_preflight_sm_host_uses_live_sm_table_when_inventory_is_stale`
      (Part 1 regression)
- [x] **T2** — RED: `test_preflight_sm_host_does_not_override_when_inventory_matches`
      (no-false-positives pin)
- [x] **T3** — RED: `test_preflight_tier3_substring_fallback_behavior_unchanged`
      (must assert the **post-override** contract: probe destination is the
      AP-reported IP. Asserting on the resolver alone passes before and after
      the fix and pins nothing)
- [x] **T4** — GREEN: implement the Part 1 host override; T1–T3 green
- [x] **T5** — RED: `test_register_device_translates_all_wire_failures_to_typed`
- [x] **T6** — GREEN: implement Part 3 `except DriverError`; T5 green
- [x] **T7** — Part 2 prompt-conformance step in `netops_orchestrator.md`
- [x] **T8** — Full applicable checks + work-unit commit
- [x] **T9** — PR with the operations note (below)

## Route per task

Delegated writer for T1–T7 (touches 3 files with non-trivial edits).
T8–T9 inline (state + delivery, already-understood).

## TDD

**Mode: enabled** (Strict TDD Mode, project config).
**Runner:** `make test-one K=<pattern>` (pytest, `.venv/bin/python`, Python 3.12.14).
RED before GREEN on every task. No invented evidence.

## Acceptance criteria

- [x] `tests/test_snmp_migrate.py::test_preflight_sm_host_uses_live_sm_table_when_inventory_is_stale`
- [x] `tests/test_snmp_migrate.py::test_preflight_sm_host_does_not_override_when_inventory_matches`
- [x] `tests/test_snmp_migrate.py::test_preflight_tier3_substring_fallback_behavior_unchanged`
- [x] `tests/test_register_device.py::test_register_device_translates_all_wire_failures_to_typed`
- [x] The four `test_preflight_raises_when_*` tests still pass
- [x] Zero new tool parameters, state, config flags, or tool verbs

## Operations note for the PR description

Part 1 converts previously-aborting migrations into proceeding ones. APs blocked for
months may migrate on the next operator request, against SMs nobody has re-validated.
Without this note, the next `CommunityValidationFailed` reads as a regression.

## Blast radius

`migrate_subscriber` (`migrate.py:757-777`) receives a LUID and logs but emits **no
per-SM SET frame** — the per-SM migration is a stub. Part 1's blast radius is
therefore read-only today: the pre-flight probe and nothing else.

## Progress

T1–T8 complete. T7 resolved as a no-op: `netops_orchestrator.md:56-63` already
instructs the `register_device`-then-migrate flow, so no prompt change was needed.

### Verification evidence (observed by the orchestrator, not reported)

**RED confirmed independently** — stashed both source files, re-ran the new tests:
```
FAILED tests/test_register_device.py::test_register_device_translates_all_wire_failures_to_typed
FAILED tests/test_snmp_migrate.py::test_preflight_sm_host_uses_live_sm_table_when_inventory_is_stale
FAILED tests/test_snmp_migrate.py::test_preflight_tier3_substring_fallback_behavior_unchanged
3 failed, 936 deselected
```
Fix restored → `4 passed, 935 deselected`. The tests genuinely drive the fix.

**T2 note:** `test_preflight_sm_host_does_not_override_when_inventory_matches` was
green before and after by design — it is a no-false-positives pin, not a RED test.
Its RED was a test-authoring artifact (the assertion initially captured AP-side calls);
the writer narrowed the filter. It does not claim to drive the fix.

**Applicable checks:**
- `.venv/bin/python -m pytest --no-cov -n auto -m "not no_xdist"` → **895 passed, 4 skipped, 1 xfailed, 1 xpassed**
- `.venv/bin/python -m ruff check src/ tests/` → All checks passed
- `.venv/bin/python -m ruff format --check src/ tests/` → 145 files already formatted

One deviation found and fixed: the writer did not run `ruff format`, which failed
`tests/test_toolchain.py::test_ruff_format_check_exits_zero_on_clean_tree` on the full
suite. Formatted; suite then green.

## Next step

None. T9 complete: PR #99 squash-merged to `main` as `aff9cd2` on
2026-09-28, CI `test` job SUCCESS. Released as **v0.3.11**.

Delivery was `disabled/unmanaged` — receipt-driven development is off at
clone-local scope and was not enabled. Merge and release were the user's
explicit decision.

Two out-of-scope items surfaced during delivery, neither acted on:
- `feat/issue-80-migrate-writable-wiring` (unpushed, 91-line `migrate.py` diff)
  and `fix/issue-89-stage-literal` both touch `migrate.py` and will conflict
  at merge time.
- The `branch-pr` skill's `status:approved` / `type:*` label requirements do
  not exist in this repo; CI is lint/format/mypy/test only.

