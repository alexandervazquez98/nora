# Issue #43 — Verbatim Capture (snapshot 2026-09-15)

> Captured via `gh issue view 43 --json body,title,labels,comments --repo alexandervazquez98/nora`
> on 2026-09-15. See `proposal.md` for the proposal built from this content.

**Title:** `rfc(governance/prompting): 3-tier tool service-impact classification and modular tool-spec architecture for orchestrators`

**Labels:** `documentation`, `enhancement`

**Repo:** `alexandervazquez98/nora`

## Body

In operational Network Operations Center (NOC) environments, invoking network tools without an explicit service-impact assessment poses serious risks of unintentional service outages.

For instance, while NORA MCP's `snmp_run_spectrum_analysis` currently performs a passive SNMP GET of 3 noise floor OIDs, in real-world wireless hardware (e.g., Cambium PMP 450i), an active spectrum analysis drops sector transmission and disassociates subscriber modules. An LLM orchestrator executing `snmp_run_spectrum_analysis` autonomously during a routine diagnostic prompt can cause perceived or actual operational disruption.

We propose:

1. Standardizing a **3-Tier Service Impact Classification** across all NORA MCP tools.
2. Introducing a **Modular Tool Specification Architecture** (`docs/tool_specs/<tool_name>.md`) detailing operational tiers, prerequisites, success factors, and failure criteria.
3. Decoupling the **Orchestrator System Prompt** into a lean governance framework with an explicit **Service Impact & Disruption Gate**.

### 1. 3-Tier Tool Service Impact Classification

All NORA MCP tools are formally classified into three operational tiers:

| Tier | Category | Operational Impact | Governance Policy |
| :--- | :--- | :--- | :--- |
| **Tier 0** | **Passive Telemetry (Read-Only)** | Zero impact on subscriber traffic; non-disruptive query. | **Direct Execution**: The orchestrator or sub-agent may invoke immediately to gather telemetry. |
| **Tier 1** | **Potentially Disruptive / Active Telemetry** | Potential CPU load, active RF sweeping, or intrusive probing. | **Pause & Clearance Gate**: The agent MUST explain the technical necessity and request operator clearance before invocation. |
| **Tier 2** | **Service-Affecting Mutations (Write / Config)** | Interrupts RF link, drops SMs, alters carrier frequency, changes power, reboots. | **Strict HITL Gate**: Absolute halt. Requires valid ticket number, maintenance window, and cryptographic/explicit HITL token. |

**Tool Mapping:**

* **Tier 0 (Passive Reads):**
  * `snmp_get_ap_summary`
  * `snmp_get_sm_table`
  * `snmp_get_pmp450i_radio_metrics`
  * `snmp_get_frame_utilization`
  * `snmp_get_sm_detailed_diagnostics`
  * `search_intervention_history`
  * `get_device_lifecycle_summary`
  * `correlate_sector_interference`
* **Tier 1 (Potentially Disruptive / Clearance Required):**
  * `snmp_run_spectrum_analysis` (Must pause and ask for operator clearance unless explicitly requested).
* **Tier 2 (Mutations / HITL Token Required):**
  * `snmp_migrate_radio_frequency`
  * `save_intervention_record`

### 2. Modular Tool Specifications (`docs/tool_specs/`)

Instead of overloading the orchestrator's system prompt with brittle vendor/tool-specific instructions, each tool has an isolated specification file in English under `docs/tool_specs/`:

* Operational classification & Tier.
* Input requirements and typing.
* Pre-flight checks (e.g. firmware catalog compatibility).
* Interpretation thresholds (e.g., CINR, noise floor dBm, frame utilization percentages).
* Error handling and operator escalation protocols.

Files authored:

* `docs/tool_specs/README.md`
* `docs/tool_specs/snmp_get_ap_summary.md`
* `docs/tool_specs/snmp_get_sm_table.md`
* `docs/tool_specs/snmp_get_pmp450i_radio_metrics.md`
* `docs/tool_specs/snmp_get_frame_utilization.md`
* `docs/tool_specs/snmp_get_sm_detailed_diagnostics.md`
* `docs/tool_specs/search_intervention_history.md`
* `docs/tool_specs/get_device_lifecycle_summary.md`
* `docs/tool_specs/correlate_sector_interference.md`
* `docs/tool_specs/snmp_run_spectrum_analysis.md`
* `docs/tool_specs/snmp_migrate_radio_frequency.md`
* `docs/tool_specs/save_intervention_record.md`

### 3. Orchestrator Governance & Operator Interaction Protocol

The Orchestrator system prompt incorporates the **Universal Service Impact & Disruption Gate**:

1. When asked for general diagnostics, the orchestrator directly executes Tier 0 tools to establish a baseline.
2. If the orchestrator discovers degraded subscribers and considers running a spectrum analysis (Tier 1), it **pauses execution**, explains the rationale, and prompts the human operator for clearance.
3. If parameters (device ID, clearance, ticket number, HITL approval token) are missing, the orchestrator halts and requests them politely and concisely rather than hallucinating or proceeding blindly.

### Verification & Prototype

A working prototype of this governance structure has been deployed and validated in Open WebUI test environment (Port 3001) interfacing with `nora-mcp`:

* Validated that routine diagnostic queries execute Tier 0 tools without unprompted spectrum sweeps.
* Validated that reasoning models (Qwen 3.5 / DeepSeek) cleanly delimit thoughts within `<think>...</think>` and emit visible tables in the operator's response.

## Owner Comment (2026-09-15 — `alexandervazquez98`)

Thanks for the thorough breakdown and architectural assessment. Your observation is spot on: **relying solely on prompt-level compliance for operational gates is insufficient for production NOC environments** — defense-in-depth requires deterministic server-side enforcement.

Here is the architectural alignment on the open questions:

### 1. Scope Decision: Scope B+ (RFC + Dynamic Composition + Server-Side Enforcement in NORA)

We should proceed with **Scope B enriched with Server-Side Guards in `nora-mcp`**, while **strictly keeping Open WebUI decoupled**:

* **Open WebUI Boundary**: Open WebUI is purely an external MCP client consumer. The NORA repository should NOT vendor, fork, or patch Open WebUI internals. All safety mechanisms must be self-contained within `nora-mcp` using standard JSON-RPC MCP semantics. This ensures NORA is safe regardless of whether the client is Open WebUI, Claude Desktop, or an automated agent.

### 2. Resolution of Unclarified RFC Points

* **Prompt Injection & Serving**:
  * `src/nora/prompts/registry.py` (`PromptRegistry`) remains the single source of truth, loading `netops_orchestrator.md` at boot time and serving it via `@mcp.prompt netops_orchestrator`.
  * The prompt can be dynamically composed at boot to append the capability matrix from `docs/tool_specs/`.
* **Tier 1 Clearance Materialization (Server-Side Guard)**:
  * To prevent the LLM from invoking `snmp_run_spectrum_analysis` autonomously, add an explicit confirmation parameter to the tool:
    ```python
    @mcp.tool
    def snmp_run_spectrum_analysis(device_id: str, operator_confirmed: bool = False) -> dict[str, Any]:
        if not operator_confirmed:
            raise ClearanceRequiredError("Tier 1 Potentially Disruptive: Spectrum analysis requires operator_confirmed=True.")
        ...
    ```
  * In chat, the orchestrator converses with the human, explains why spectrum analysis is needed, and only passes `operator_confirmed=True` once the human explicitly approves. If the LLM tries to call it prematurely with `False` (or defaults), NORA halts execution at the server level.
* **Tier 2 HITL Token Emission & Validation**:
  * Validation is already implemented in `nora.hitl.tokens.verify_approval_token`.
  * For emission, we should expose a simple operator CLI command: `nora hitl mint --operator-id <id> --ttl-seconds 900` so human NOC engineers can mint verifiable HMAC tokens to paste into the chat when authorizing disruptive migrations.

### Proposed Next Step for SDD

Let's define the SDD specification around:

1. Standardizing the 3-tier taxonomy and checking in `docs/tool_specs/*.md`.
2. Refactoring `src/nora/prompts/netops_orchestrator.md` with the Service Impact Gate.
3. Adding the `operator_confirmed: bool = False` server-side gate to `snmp_run_spectrum_analysis` (Tier 1).
4. CLI token minting utility for Tier 2 HITL workflows.

---

## Explore-Phase Correction To Surface

> **The owner's comment says "Validation is already implemented in `nora.hitl.tokens.verify_approval_token`" — and later references "verifiable HMAC tokens".**
>
> **Explore-phase code reading finds that `verify_approval_token` does NOT validate an HMAC signature.** It validates JSON shape + expiry only. Tokens are trivially forgeable today. The premise "HMAC validation already exists in `verify_approval_token`" is incorrect at the code level — HMAC exists for **OID catalogs** (`drivers/oid_catalog.py:33,535`), not for HITL tokens.
>
> The proposal phase MUST decide: (a) add HMAC-SHA256 to `HitlApprovalToken` mirroring the catalog pattern (recommended), or (b) ship the `mint` CLI on top of the current stub and document the forgeability as a known limitation.