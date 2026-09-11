# prompt-registry Specification

## Purpose

Defines how NORA loads system prompts at boot. The `PromptRegistry` reads Markdown files from package data (or an operator override directory), validates a front-matter contract, and exposes a stable per-session lookup. Hot-reload is forbidden; a missing prompt at boot is fatal. The first shipped prompt, `snmp_pmp450i.md`, instructs the LLM about the typed `RadioMetricsReport` schema and zero-leakage rules.

## Requirements

### Requirement: One-Shot Boot Load

The `PromptRegistry` MUST load all prompts exactly once at boot. After boot, the loaded set MUST be immutable for the lifetime of the process. Hot-reload and inotify watches MUST NOT exist.

#### Scenario: registry loads once and never reloads

- GIVEN a running process whose registry has already loaded
- WHEN the operator writes a new prompt file into the prompts directory
- THEN the registry does not pick up the change

### Requirement: Default Source — Package Data

By default, the registry MUST read Markdown files from `src/nora/prompts/*.md` shipped with the wheel. Each `.md` MUST declare front-matter with `name` and `description` fields.

#### Scenario: packaged prompt loads on boot

- GIVEN `src/nora/prompts/snmp_pmp450i.md` exists with valid front-matter
- WHEN boot instantiates the registry
- THEN `registry.get("snmp_pmp450i")` returns the parsed body and `description` is preserved

### Requirement: Operator Override — `NORA_PROMPTS_DIR`

An operator MAY override the source directory via `Settings.prompts_dir` (no `os.environ` calls inline). The override MUST be honoured at boot and MUST take precedence over packaged data.

#### Scenario: override directory wins over package data

- GIVEN `Settings.prompts_dir == "/etc/nora/prompts"` containing a `snmp_pmp450i.md` whose body says "OVERRIDDEN"
- WHEN boot instantiates the registry
- THEN `registry.get("snmp_pmp450i")` returns the override body

### Requirement: Fail-Closed on Missing Prompt

A prompt referenced at boot that cannot be located MUST raise a typed `PromptNotFoundError`. The registry MUST NOT substitute a default or empty string.

#### Scenario: missing prompt aborts boot

- GIVEN `Settings.prompts_dir == "."`, no packaged copy exists, and `.` contains no `snmp_pmp450i.md`
- WHEN boot instantiates the registry
- THEN a typed `PromptNotFoundError` is raised
- AND the server does not start

### Requirement: Front-Matter Schema Validation

Every prompt file MUST begin with YAML front-matter. The `name` field MUST match the file's basename (sans `.md`); the `description` field MUST be a non-empty string. Validation failures raise `PromptNotFoundError`.

#### Scenario: a file without front-matter is rejected

- GIVEN a `foo.md` with no front-matter block
- WHEN boot validates it
- THEN a typed `PromptNotFoundError` is raised
- AND the file is NOT registered

#### Scenario: an empty description is rejected

- GIVEN a `foo.md` whose front-matter has `description: ""`
- WHEN boot validates it
- THEN a typed `PromptNotFoundError` is raised

### Requirement: Stable Per-Session Reference

The registry MUST return equal content for the same name within a single process. The second `registry.get(name)` call MUST NOT trigger I/O.

#### Scenario: identical name returns identical content without I/O

- GIVEN an active process
- WHEN `registry.get("snmp_pmp450i")` is called twice
- THEN both returned bodies compare equal
- AND no I/O is performed on the second call

### Requirement: `snmp_pmp450i.md` System Prompt

The shipped `snmp_pmp450i.md` MUST describe the `snmp_get_pmp450i_radio_metrics` tool, document the typed `RadioMetricsReport` schema (no `dict` or `Any`), and reinforce zero-leakage rules: no real IPs, MACs, serials, hostnames, or credentials in any LLM-visible output.

#### Scenario: shipped prompt declares tool name and typed schema

- GIVEN `src/nora/prompts/snmp_pmp450i.md`
- WHEN a content scan looks for the tool name and the phrase "RadioMetricsReport"
- THEN both are present
- AND the file contains no private IPv4 literal, MAC literal, or hostname literal

## Cross-References

- `secure-configuration` — `prompts_dir` follows the additive `Settings` pattern; env-var surface inherits the locked-no-hardcoded-values rule.
- `telemetry-sanitizer` — the registry MUST NOT mutate prompt bodies through the sanitizer.
- `driver-snmp-pmp450i` — the registered prompt is the system context used when the LLM plans a driver call.
