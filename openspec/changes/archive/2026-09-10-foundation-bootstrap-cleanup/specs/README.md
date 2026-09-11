# foundation-bootstrap-cleanup — Spec Delta Decision

**Verdict**: No spec text modified. No delta spec required.

This change closes 4 PARTIAL scenarios from
`openspec/changes/archive/2026-09-06-foundation-bootstrap/verify-report.md`,
tightens `_SDK_ERROR_MAP` (implementation detail), and refreshes
`openspec/config.yaml` prose plus `coverage_threshold`. Each PARTIAL scenario
already maps to an existing SHOULD/MUST requirement in
`openspec/specs/{name}/spec.md`; the new tests exercise that requirement. W3
is implementation tightening; R1/R4 are config-only. There is NO spec
behaviour change.

## Capabilities Touched (5)

| Capability | Spec file | Reason touched | Spec delta? |
|------------|-----------|----------------|-------------|
| `project-toolchain` | `openspec/specs/project-toolchain/spec.md` | PARTIAL: Discoverable Make — behaviour test missing | None — requirement already exists |
| `nora-mcp-server` | `openspec/specs/nora-mcp-server/spec.md` | PARTIAL: error-path sanitization + secrets-leak | None — requirements already exist |
| `telemetry-sanitizer` | `openspec/specs/telemetry-sanitizer/spec.md` | PARTIAL: alias-map log-level contract not asserted | None — requirement already exists |
| `secure-configuration` | `openspec/specs/secure-configuration/spec.md` | PARTIAL: `model_id` reaches SDK verbatim | None — requirement already exists |
| `llm-provider-interface` | `openspec/specs/llm-provider-interface/spec.md` | W3: explicit `_SDK_ERROR_MAP` typing | None — implementation detail, spec silent on mechanism |

## PARTIAL Scenario -> Spec Requirement Mapping

| # | PARTIAL scenario | Spec file / line | Requirement heading | Existing requirement text (verbatim) |
|---|------------------|------------------|---------------------|----------------------------------------|
| 1 | `project-toolchain` Discoverable Make behaviour | `project-toolchain/spec.md:107` | `Discoverable Make Targets` | "A `Makefile` MUST expose `make test`, `make lint`, `make type`, `make format`, and `make run` that wrap the configured commands." Scenario `:111-114`: "make test wraps the configured runner". |
| 2 | `nora-mcp-server` error-path sanitization + secrets leak | `nora-mcp-server/spec.md:73` and `:89` | `Telemetry Sanitizer Boundary` + `Security Boundary - No Secrets in Tool Responses` | "Every free-text value leaving the process via an MCP tool response MUST pass through the telemetry sanitizer before serialisation." + "Every MCP tool MUST NOT include any secret value from `Settings` in any response field, error message, or log line." |
| 3 | `telemetry-sanitizer` alias-map log-level contract | `telemetry-sanitizer/spec.md:120` | `Observability - Replacement Counter` | "the alias mapping table MUST NOT be logged at `INFO` or higher". Scenario `:124-126`: "the alias map is absent from log lines AND only the per-category counter MAY appear at `DEBUG`". |
| 4 | `secure-configuration` `model_id` untouched | `secure-configuration/spec.md:112` | `Telemetry Path Sanitization Boundary` | "configuration values MUST never be passed to the sanitizer as user content". Scenario `:116-119`: "`model_id` is passed verbatim to the SDK AND the sanitizer is NOT applied to `model_id`". |

## W3 (`_SDK_ERROR_MAP`) Rationale

`openspec/specs/llm-provider-interface/spec.md:44` states:

> "`LMStudioProvider` MUST use the native `lmstudio` SDK..., and MUST map SDK
> errors to `LLM_UNAVAILABLE`."

The spec describes the WHAT (any SDK error -> `LLM_UNAVAILABLE`) but is silent
on the HOW (the catch-tuple contents). Replacing `(Exception,)` with explicit
SDK exception classes is implementation tightening; the behaviour contract is
unchanged. `KeyboardInterrupt` / `SystemExit` NOT being caught is standard
Python semantics, not a new spec requirement. Per sdd-spec rule: "DO NOT
include implementation details in specs - specs describe WHAT, not HOW."

## R1 / R4 Rationale

R1 and R4 modify `openspec/config.yaml` only (`context:`, `testing:` prose and
`coverage_threshold: 85`). Spec text is untouched. The
`project-toolchain/spec.md:28` requirement that "the coverage threshold MUST
start at 0 until a later change raises it" explicitly permits raising it; the
config field carries the live value.

## Conclusion

- No `## ADDED Requirements`
- No `## MODIFIED Requirements`
- No `## REMOVED Requirements`
- No `## RENAMED Requirements`

The 4 new tests, `_SDK_ERROR_MAP` enumeration, and config refresh do not
require spec text changes. Archive will leave `openspec/specs/{name}/spec.md`
unchanged.
