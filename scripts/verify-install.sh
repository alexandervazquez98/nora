#!/usr/bin/env bash
# scripts/verify-install.sh
#
# Post-install health check for NORA MCP.
#
# Verifies the install shape documented in INSTALL.md and OPERATIONS.md:
# binaries present + version-pinned, paths + files exist, signing_key has
# mode 0600 owned by nora:nora, nora.env has mode 0640 (or 0600), systemd
# is active, and the running daemon responds over JSON-RPC with the
# canonical 11 tools + 2 prompts. The 11 tools are the unified NetOps
# radio surface shipped via the ``2026-09-13-pmp450i-production-surface``
# cluster (PRs #26-#29) plus the writer sibling `save_intervention_record`
# (PR #25). The 2 prompts are ``netops_orchestrator`` and ``snmp_pmp450i``.
#
# Exit codes:
#   0  all OK or all WARN (default mode)
#   1  at least one FAIL
#   2  --strict and at least one WARN
#
# Usage:
#   sudo scripts/verify-install.sh                       # human-readable table
#   sudo scripts/verify-install.sh --json                # machine-readable JSON
#   sudo scripts/verify-install.sh --strict              # exit 2 on any WARN
#   sudo scripts/verify-install.sh --skip-systemd
#   sudo scripts/verify-install.sh --skip-functional
#
# Pass --prefix / --config-dir / --state-dir / --log-dir / --user to point
# the verifier at a non-default install (useful for tests).

set -euo pipefail
IFS=$'\n\t'
# ERR trap — same convention as install.sh: print the failing line and
# propagate the failing command's status.
trap 'printf "%s failed at line %s\n" "${SCRIPT_NAME:-verify-install.sh}" "${LINENO}" >&2' ERR

# ---------------------------------------------------------------------------
# Constants — self-locate the script.
# ---------------------------------------------------------------------------
SCRIPT_PATH="${BASH_SOURCE[0]}"
if command -v readlink >/dev/null 2>&1 && readlink -f "${SCRIPT_PATH}" >/dev/null 2>&1; then
    SCRIPT_PATH="$(readlink -f "${SCRIPT_PATH}")"
elif command -v realpath >/dev/null 2>&1; then
    SCRIPT_PATH="$(realpath "${SCRIPT_PATH}")"
fi
SCRIPT_DIR="$(dirname "${SCRIPT_PATH}")"
SCRIPT_NAME="$(basename "${SCRIPT_PATH}")"
# Export SCRIPT_NAME so child processes (e.g., the trap handler if it
# ever needs to log a path) can reference it. SCRIPT_DIR is also exported
# to keep the surface uniform with install.sh.
export SCRIPT_DIR SCRIPT_NAME

# ---------------------------------------------------------------------------
# Defaults — overridable via flags.
# ---------------------------------------------------------------------------
DEFAULT_USER="nora"
DEFAULT_PREFIX="/opt/nora"
DEFAULT_CONFIG_DIR="/etc/nora"
DEFAULT_STATE_DIR="/var/lib/nora"
DEFAULT_LOG_DIR="/var/log/nora"

USER_NAME="${DEFAULT_USER}"
PREFIX="${DEFAULT_PREFIX}"
CONFIG_DIR="${DEFAULT_CONFIG_DIR}"
STATE_DIR="${DEFAULT_STATE_DIR}"
LOG_DIR="${DEFAULT_LOG_DIR}"

STRICT=0
JSON_OUT=0
SKIP_FUNCTIONAL=0
SKIP_SYSTEMD=0
CHECK_HTTP=0

# CHECK_RESULTS holds one entry per check in the form:
#   name|status|detail
# Status is one of: ok, warn, fail.
CHECK_RESULTS=()

# ---------------------------------------------------------------------------
# Helpers — logging + JSON parsing delegated to python3 (no `jq` dep).
# ---------------------------------------------------------------------------

log() {
    printf '[OK] %s\n' "$1" >&2
}

warn() {
    printf '[WARN] %s\n' "$1" >&2
}

fail() {
    printf '[FAIL] %s\n' "$1" >&2
}

# record_check <name> <status> <detail>
# Append a check result to CHECK_RESULTS. `name` and `detail` may contain
# spaces and arbitrary chars; the parser splits on the first two `|`.
record_check() {
    local name="$1"
    local status="$2"
    local detail="$3"
    CHECK_RESULTS+=("${name}|${status}|${detail}")
}

# json_get <json_string> <python_expr_on_data>
# Print the result of `eval(<expr>, {"data": data})` on the parsed JSON.
# Exits non-zero on parse error.
json_get() {
    local json_str="$1"
    local expr="$2"
    python3 - "$json_str" "$expr" <<'PY'
import json
import sys

raw = sys.argv[1]
expr = sys.argv[2]
try:
    data = json.loads(raw)
except Exception as exc:
    print(f"json parse error: {exc}", file=sys.stderr)
    sys.exit(1)
# The expression is sourced from a hard-coded string in this script, never
# from user input — safe to eval in a locked-down namespace.
result = eval(expr, {"data": data, "__builtins__": {}})  # noqa: S307
if result is None:
    sys.exit(0)
if isinstance(result, (list, tuple)):
    print(" ".join(str(x) for x in result))
else:
    print(result)
PY
}

# json_escape <string>
# Minimal JSON string escape (backslash + double-quote). Sufficient for
# check names + detail text; does not handle control chars because those
# never appear in our check output.
json_escape() {
    local s="$1"
    s="${s//\\/\\\\}"
    s="${s//\"/\\\"}"
    printf '%s' "${s}"
}

# ---------------------------------------------------------------------------
# usage + parse_args
# ---------------------------------------------------------------------------

usage() {
    cat <<USAGE >&2
Usage: sudo scripts/verify-install.sh [options]

Post-install health check for NORA MCP.

Options:
  --prefix PATH           Install prefix (default: ${DEFAULT_PREFIX}).
  --config-dir PATH       Runtime config dir (default: ${DEFAULT_CONFIG_DIR}).
  --state-dir PATH        Mutable state dir (default: ${DEFAULT_STATE_DIR}).
  --log-dir PATH          Log dir (default: ${DEFAULT_LOG_DIR}).
  --user NAME             Service account (default: ${DEFAULT_USER}).
  --json                  Emit machine-readable JSON to stdout.
  --strict                Exit non-zero on any WARN (not just FAIL).
  --skip-functional       Skip the JSON-RPC live probe.
  --skip-systemd          Skip the systemd is-active check.
  --check-http            Probe the HTTP listener when transport != stdio.
                          Default off so stdio-only operators see no change.
  --help                  Show this help and exit 0.

Exit codes:
  0  OK (or WARN-only, default mode)
  1  at least one FAIL
  2  --strict and at least one WARN
USAGE
}

parse_args() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --prefix)
                [ "$#" -ge 2 ] || { fail "--prefix requires an argument"; exit 2; }
                PREFIX="$2"; shift
                ;;
            --config-dir)
                [ "$#" -ge 2 ] || { fail "--config-dir requires an argument"; exit 2; }
                CONFIG_DIR="$2"; shift
                ;;
            --state-dir)
                [ "$#" -ge 2 ] || { fail "--state-dir requires an argument"; exit 2; }
                STATE_DIR="$2"; shift
                ;;
            --log-dir)
                [ "$#" -ge 2 ] || { fail "--log-dir requires an argument"; exit 2; }
                LOG_DIR="$2"; shift
                ;;
            --user)
                [ "$#" -ge 2 ] || { fail "--user requires an argument"; exit 2; }
                USER_NAME="$2"; shift
                ;;
            --json) JSON_OUT=1 ;;
            --strict) STRICT=1 ;;
            --skip-functional) SKIP_FUNCTIONAL=1 ;;
            --skip-systemd) SKIP_SYSTEMD=1 ;;
            --check-http) CHECK_HTTP=1 ;;
            --help|-h) usage; exit 0 ;;
            --) shift; break ;;
            -*) fail "unknown flag: $1"; usage; exit 2 ;;
            *) fail "unexpected positional argument: $1"; usage; exit 2 ;;
        esac
        shift
    done
}

# ---------------------------------------------------------------------------
# Individual checks — each returns 0 (OK), 1 (WARN), 2 (FAIL). The return
# value is captured by main and aggregated into the final exit code, but
# the script does NOT abort on a non-zero return because of the `|| true`
# callsites in main.
# ---------------------------------------------------------------------------

check_binaries() {
    local rc=0

    # python3 >= 3.12 — this is the runtime; if it's missing or old,
    # nothing else can run. WARN on <3.12, FAIL on missing.
    if ! command -v python3 >/dev/null 2>&1; then
        record_check "binaries.python3" "fail" "python3 not on PATH"
        rc=2
    elif ! python3 -c "import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)" 2>/dev/null; then
        local ver
        ver="$(python3 --version 2>&1 || true)"
        record_check "binaries.python3" "warn" "${ver} (< 3.12)"
        rc=1
    else
        record_check "binaries.python3" "ok" "$(python3 --version 2>&1)"
    fi

    # uv — required by install + day-2 ops.
    if ! command -v uv >/dev/null 2>&1; then
        record_check "binaries.uv" "fail" "uv not on PATH"
        rc=2
    else
        record_check "binaries.uv" "ok" "$(uv --version 2>&1)"
    fi

    # git — required for phase_install.
    if ! command -v git >/dev/null 2>&1; then
        record_check "binaries.git" "fail" "git not on PATH"
        rc=2
    else
        record_check "binaries.git" "ok" "$(git --version 2>&1)"
    fi

    # systemctl — required for the bundled unit. FAIL when missing.
    if ! command -v systemctl >/dev/null 2>&1; then
        record_check "binaries.systemctl" "fail" "systemctl not on PATH"
        rc=2
    else
        record_check "binaries.systemctl" "ok" "$(systemctl --version 2>&1 | head -1)"
    fi

    return "${rc}"
}

check_paths() {
    local rc=0
    local p
    for p in "${PREFIX}" "${CONFIG_DIR}" "${LOG_DIR}" "${STATE_DIR}/interventions"; do
        if [ ! -e "${p}" ]; then
            record_check "paths.${p}" "fail" "missing"
            rc=2
        else
            record_check "paths.${p}" "ok" "exists"
        fi
    done
    return "${rc}"
}

check_files() {
    local rc=0
    local nora_mcp="${PREFIX}/.venv/bin/nora-mcp"
    local env_file="${CONFIG_DIR}/nora.env"
    local key_file="${CONFIG_DIR}/signing_key"

    if [ ! -e "${nora_mcp}" ]; then
        record_check "files.nora-mcp" "fail" "${nora_mcp} missing"
        rc=2
    elif [ ! -x "${nora_mcp}" ]; then
        record_check "files.nora-mcp" "fail" "${nora_mcp} not executable"
        rc=2
    else
        record_check "files.nora-mcp" "ok" "executable"
    fi

    if [ ! -f "${env_file}" ]; then
        record_check "files.nora.env" "fail" "${env_file} missing"
        rc=2
    else
        record_check "files.nora.env" "ok" "present"
    fi

    if [ ! -f "${key_file}" ]; then
        record_check "files.signing_key" "fail" "${key_file} missing"
        rc=2
    else
        record_check "files.signing_key" "ok" "present"
    fi
    return "${rc}"
}

# Cross-platform mode+owner reader. GNU stat (Linux) takes -c '%a %U:%G';
# BSD stat (macOS) takes -f '%Lp %Su:%Sg'. Detect at call time so this
# works on both.
stat_mode_owner() {
    local path="$1"
    if stat -c '%a %U:%G' "${path}" >/dev/null 2>&1; then
        stat -c '%a %U:%G' "${path}"
    else
        stat -f '%Lp %Su:%Sg' "${path}"
    fi
}

check_permissions() {
    local rc=0
    local key_file="${CONFIG_DIR}/signing_key"
    local env_file="${CONFIG_DIR}/nora.env"

    # Owner-string expected: "${USER_NAME}:${primary_group}". On a real
    # install `nora` is a system account whose primary group is also
    # `nora` — so the canonical `nora:nora` matches. On test hosts the
    # user may be a regular account whose primary group is `staff`,
    # `wheel`, etc.; we still accept the file because the user owns it
    # and the group is that user's primary group. The strict nora:nora
    # match is documented in OPERATIONS.md § "Store"; this check is the
    # looser "the file is in nora's namespace" interpretation that also
    # lets a developer run the verifier against a personal checkout.
    local primary_group
    primary_group="$(id -gn "${USER_NAME}" 2>/dev/null || echo "${USER_NAME}")"
    local expected_owner="${USER_NAME}:${primary_group}"

    # signing_key — must be 0600 and owned by nora (or the configured
    # USER_NAME). Mode 0600 is a hard FAIL; ownership mismatch is also FAIL.
    if [ -f "${key_file}" ]; then
        local mo mode owner
        mo="$(stat_mode_owner "${key_file}")"
        mode="${mo% *}"
        owner="${mo#* }"
        if [ "${mode}" = "600" ] && [ "${owner}" = "${expected_owner}" ]; then
            record_check "permissions.signing_key" "ok" "${mode} ${owner}"
        elif [ "${mode}" != "600" ]; then
            record_check "permissions.signing_key" "fail" "mode=${mode} (expected 600)"
            rc=2
        else
            record_check "permissions.signing_key" "fail" "owner=${owner} (expected ${expected_owner})"
            rc=2
        fi
    fi

    # nora.env — 0640 or 0600 OK; 0644 is a WARN (group-readable is the
    # documented prod target, world-readable too lax is the WARN band);
    # anything else FAIL.
    if [ -f "${env_file}" ]; then
        local mo mode owner
        mo="$(stat_mode_owner "${env_file}")"
        mode="${mo% *}"
        owner="${mo#* }"
        case "${mode}" in
            640|600)
                if [ "${owner}" = "${expected_owner}" ]; then
                    record_check "permissions.nora.env" "ok" "${mode} ${owner}"
                else
                    record_check "permissions.nora.env" "warn" "owner=${owner} (expected ${expected_owner})"
                    rc=1
                fi
                ;;
            644)
                record_check "permissions.nora.env" "warn" "mode=${mode} (group-readable; tighten to 0640)"
                rc=1
                ;;
            *)
                record_check "permissions.nora.env" "fail" "mode=${mode} is too permissive"
                rc=2
                ;;
        esac
    fi
    return "${rc}"
}

check_systemd() {
    if ! command -v systemctl >/dev/null 2>&1; then
        record_check "systemd.active" "fail" "systemctl not on PATH"
        return 2
    fi
    local state
    state="$(systemctl is-active nora-mcp 2>/dev/null || echo unknown)"
    case "${state}" in
        active)
            record_check "systemd.active" "ok" "active"
            return 0
            ;;
        inactive)
            record_check "systemd.active" "warn" "inactive"
            return 1
            ;;
        failed)
            record_check "systemd.active" "fail" "failed"
            return 2
            ;;
        *)
            record_check "systemd.active" "warn" "state=${state}"
            return 1
            ;;
    esac
}

check_functional() {
    # Live JSON-RPC probe. Spawns the daemon with sudo -u USER, sends the
    # canonical four-frame handshake (initialize → notifications/initialized
    # → tools/list → prompts/list), and validates the three expected
    # responses. protocolVersion 2025-06-18 matches INSTALL.md's smoke
    # test.
    local nora_mcp="${PREFIX}/.venv/bin/nora-mcp"
    local tmp_out tmp_err

    if [ ! -x "${nora_mcp}" ]; then
        record_check "functional.jsonrpc" "fail" "${nora_mcp} not executable"
        return 2
    fi
    if ! command -v sudo >/dev/null 2>&1; then
        record_check "functional.jsonrpc" "fail" "sudo not on PATH"
        return 2
    fi

    tmp_out="$(mktemp)"
    tmp_err="$(mktemp)"
    # Belt-and-braces: ensure cleanup even if the script aborts mid-check.
    trap 'rm -f "${tmp_out:-}" "${tmp_err:-}"' RETURN

    # Spawn nora-mcp with the four JSON-RPC frames on stdin. protocolVersion
    # 2025-06-18 is the MCP protocol version this server implements; the
    # daemon uses it as a static handshake value.
    # shellcheck disable=SC2024
    {
        printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"verify","version":"0"}}}'
        printf '%s\n' '{"jsonrpc":"2.0","method":"notifications/initialized"}'
        printf '%s\n' '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
        printf '%s\n' '{"jsonrpc":"2.0","id":3,"method":"prompts/list"}'
    } | sudo -n -u "${USER_NAME}" "${nora_mcp}" > "${tmp_out}" 2> "${tmp_err}" &

    local pid=$!

    # Wait up to 5 seconds for the daemon to finish handling all four
    # frames (it blocks on stdin until EOF, then exits).
    local _i
    for _i in 1 2 3 4 5; do
        if ! kill -0 "${pid}" 2>/dev/null; then break; fi
        sleep 1
    done
    if kill -0 "${pid}" 2>/dev/null; then
        kill "${pid}" 2>/dev/null || true
        wait "${pid}" 2>/dev/null || true
        record_check "functional.jsonrpc" "fail" "nora-mcp timed out after 5s"
        return 2
    fi
    wait "${pid}" 2>/dev/null || true

    # Parse each response line by id and validate against the expected
    # schema. The expected tool + prompt names mirror what
    # `tests/test_sign_catalog.py` and the smoke test in INSTALL.md
    # already pin, so any drift surfaces here.
    local init_ok=0 tools_ok=0 prompts_ok=0
    local line id name actual expected
    while IFS= read -r line; do
        [ -z "${line}" ] && continue
        id="$(json_get "${line}" 'data.get("id", "")' 2>/dev/null || echo "")"
        case "${id}" in
            1)
                # initialize → serverInfo.name must be "nora".
                name="$(json_get "${line}" 'data.get("result", {}).get("serverInfo", {}).get("name", "")' 2>/dev/null || echo "")"
                if [ "${name}" = "nora" ]; then init_ok=1; fi
                ;;
            2)
                # tools/list → exactly the twelve canonical tool names.
                # Order matches FastMCP's registration order in server.py
                # (NOT alphabetical). The PRs that added slices 2/3/4
                # appended to the registration list, so the radio-metrics
                # + intervention lifecycle set from PRs #26 + #25 leads,
                # followed by the unified NetOps radio surface from
                # slices 2/3/4 (PRs #27-#29). WU-4 / PR #44 appended
                # the admin HITL token issuer `hitl_mint_token` last.
                expected="snmp_get_pmp450i_radio_metrics snmp_get_ap_summary snmp_get_frame_utilization snmp_get_sm_table snmp_get_sm_detailed_diagnostics snmp_run_spectrum_analysis snmp_migrate_radio_frequency search_intervention_history get_device_lifecycle_summary correlate_sector_interference save_intervention_record hitl_mint_token"
                actual="$(json_get "${line}" '" ".join(t.get("name", "") for t in data.get("result", {}).get("tools", []))' 2>/dev/null || echo "")"
                if [ "${actual}" = "${expected}" ]; then tools_ok=1; fi
                ;;
            3)
                # prompts/list → exactly the two canonical prompt names.
                expected="netops_orchestrator snmp_pmp450i"
                actual="$(json_get "${line}" '" ".join(p.get("name", "") for p in data.get("result", {}).get("prompts", []))' 2>/dev/null || echo "")"
                if [ "${actual}" = "${expected}" ]; then prompts_ok=1; fi
                ;;
        esac
    done < "${tmp_out}"

    if [ "${init_ok}" = "1" ] && [ "${tools_ok}" = "1" ] && [ "${prompts_ok}" = "1" ]; then
        record_check "functional.jsonrpc" "ok" "tools=12 prompts=2 serverInfo.name=nora"
        return 0
    fi
    record_check "functional.jsonrpc" "fail" "init=${init_ok} tools=${tools_ok} prompts=${prompts_ok}"
    return 2
}

check_http_listener() {
    # Opt-in HTTP smoke. Reads /etc/nora/nora-mcp.env; if transport is stdio
    # or unset, skip silently (default mode). Otherwise TCP-probe the bind
    # address + curl the MCP path. Stdout output is suppressed; only the
    # HTTP status code (or 'no-curl') is captured for the check row.
    #
    # Failure modes:
    #   * Transport != stdio AND /etc/nora/nora-mcp.env is missing → WARN
    #     (operator forgot to run install.sh or the env file was deleted).
    #   * TCP probe fails (connect refused / timeout) → FAIL.
    #   * curl is absent → WARN with `no-curl`; the TCP probe is enough
    #     evidence the socket is bound.
    local env_file="${CONFIG_DIR}/nora-mcp.env"

    if [ ! -f "${env_file}" ]; then
        record_check "http.listener" "warn" "${env_file} missing (transport probe skipped)"
        return 1
    fi

    # Source the transport env file in a sub-shell so we don't leak vars
    # into the verifier's namespace. `set -a` exports every assignment.
    # shellcheck disable=SC1090,SC1091
    local transport host port path
    transport="$(NORA_MCP_TRANSPORT="${NORA_MCP_TRANSPORT:-}" NORA_MCP_HOST="${NORA_MCP_HOST:-}" NORA_MCP_PORT="${NORA_MCP_PORT:-}" NORA_MCP_PATH="${NORA_MCP_PATH:-}" sh -c "set -a; . '${env_file}' >/dev/null 2>&1; printf '%s' \"\${NORA_MCP_TRANSPORT:-stdio}\"")"
    host="$(NORA_MCP_TRANSPORT="${NORA_MCP_TRANSPORT:-}" NORA_MCP_HOST="${NORA_MCP_HOST:-}" NORA_MCP_PORT="${NORA_MCP_PORT:-}" NORA_MCP_PATH="${NORA_MCP_PATH:-}" sh -c "set -a; . '${env_file}' >/dev/null 2>&1; printf '%s' \"\${NORA_MCP_HOST:-127.0.0.1}\"")"
    port="$(NORA_MCP_TRANSPORT="${NORA_MCP_TRANSPORT:-}" NORA_MCP_HOST="${NORA_MCP_HOST:-}" NORA_MCP_PORT="${NORA_MCP_PORT:-}" NORA_MCP_PATH="${NORA_MCP_PATH:-}" sh -c "set -a; . '${env_file}' >/dev/null 2>&1; printf '%s' \"\${NORA_MCP_PORT:-8005}\"")"
    path="$(NORA_MCP_TRANSPORT="${NORA_MCP_TRANSPORT:-}" NORA_MCP_HOST="${NORA_MCP_HOST:-}" NORA_MCP_PORT="${NORA_MCP_PORT:-}" NORA_MCP_PATH="${NORA_MCP_PATH:-}" sh -c "set -a; . '${env_file}' >/dev/null 2>&1; printf '%s' \"\${NORA_MCP_PATH:-/mcp}\"")"

    if [ "${transport}" = "stdio" ] || [ -z "${transport}" ]; then
        record_check "http.listener" "ok" "skip (transport=stdio)"
        return 0
    fi

    # TCP-probe the bind address. `/dev/tcp/<host>/<port>` is a bash
    # builtin that opens a TCP socket and exits non-zero on connect
    # refused / unreachable. We trap exit so a failed probe surfaces a
    # FAIL row, not a script abort (`set -e` is on).
    if ! bash -c "exec 3<>/dev/tcp/${host}/${port}" 2>/dev/null; then
        record_check "http.listener" "fail" "${host}:${port} unreachable"
        return 2
    fi

    if ! command -v curl >/dev/null 2>&1; then
        record_check "http.listener" "warn" "tcp ok, curl absent (no HTTP probe)"
        return 1
    fi

    local http_code
    http_code="$(curl --max-time 3 -s -o /dev/null -w '%{http_code}' "http://${host}:${port}${path}" 2>/dev/null || echo "000")"
    # 405/406/415 are expected: FastMCP rejects GET on /mcp but the socket
    # is bound. Anything in the 2xx/3xx/4xx band means the listener is up.
    case "${http_code}" in
        2*|3*|4*)
            record_check "http.listener" "ok" "tcp+http ${host}:${port}${path} -> ${http_code}"
            return 0
            ;;
        *)
            record_check "http.listener" "fail" "tcp ok, http probe returned ${http_code}"
            return 2
            ;;
    esac
}

# ---------------------------------------------------------------------------
# Report — aggregate results + emit human or JSON output.
# ---------------------------------------------------------------------------

report() {
    local ok_count=0 warn_count=0 fail_count=0
    local entry name rest status detail

    for entry in "${CHECK_RESULTS[@]}"; do
        name="${entry%%|*}"
        rest="${entry#*|}"
        status="${rest%%|*}"
        case "${status}" in
            ok) ok_count=$((ok_count + 1)) ;;
            warn) warn_count=$((warn_count + 1)) ;;
            fail) fail_count=$((fail_count + 1)) ;;
        esac
    done

    if [ "${JSON_OUT}" = "1" ]; then
        # Machine-readable JSON to stdout. Stable schema: top-level object
        # with `checks` (list) + `summary` (dict). Stable ordering: order
        # of check execution. Operators consume this via CI integrations
        # so don't add/rename fields without bumping a major version.
        printf '{"checks":['
        local first=1
        for entry in "${CHECK_RESULTS[@]}"; do
            name="${entry%%|*}"
            rest="${entry#*|}"
            status="${rest%%|*}"
            detail="${rest#*|}"
            if [ "${first}" = "1" ]; then first=0; else printf ','; fi
            printf '{"name":"%s","status":"%s","detail":"%s"}' \
                "$(json_escape "${name}")" \
                "${status}" \
                "$(json_escape "${detail}")"
        done
        printf '],"summary":{"ok":%d,"warn":%d,"fail":%d}}\n' \
            "${ok_count}" "${warn_count}" "${fail_count}"
    else
        # Human-readable table to stderr.
        printf '\n  %-30s %-6s  %s\n' "CHECK" "STATUS" "DETAIL" >&2
        printf '  %-30s %-6s  %s\n' "------------------------------" "------" "----------------------------------------" >&2
        for entry in "${CHECK_RESULTS[@]}"; do
            name="${entry%%|*}"
            rest="${entry#*|}"
            status="${rest%%|*}"
            detail="${rest#*|}"
            # Uppercase the status so the table reads [OK] / [WARN] /
            # [FAIL] at a glance — matches the prefix style used in
            # install.sh.
            case "${status}" in
                ok) status_upper="OK" ;;
                warn) status_upper="WARN" ;;
                fail) status_upper="FAIL" ;;
                *) status_upper="${status}" ;;
            esac
            printf '  %-30s %-6s  %s\n' "${name}" "${status_upper}" "${detail}" >&2
        done
        printf '\n  ok=%d warn=%d fail=%d\n\n' "${ok_count}" "${warn_count}" "${fail_count}" >&2
    fi

    if [ "${fail_count}" -gt 0 ]; then
        return 1
    fi
    if [ "${STRICT}" = "1" ] && [ "${warn_count}" -gt 0 ]; then
        return 2
    fi
    return 0
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

main() {
    parse_args "$@"

    # Every individual check is followed by `|| true` so `set -e` does
    # not abort the script on a check failure; the final `report` call
    # aggregates everything and returns the exit code that becomes the
    # script's exit code (via `set -e` propagation on the last command).
    check_binaries    || true
    check_paths       || true
    check_files       || true
    check_permissions || true

    if [ "${SKIP_SYSTEMD}" = "1" ]; then
        record_check "systemd.active" "ok" "skipped (--skip-systemd)"
    else
        check_systemd || true
    fi

    if [ "${SKIP_FUNCTIONAL}" = "1" ]; then
        record_check "functional.jsonrpc" "ok" "skipped (--skip-functional)"
    else
        check_functional || true
    fi

    if [ "${CHECK_HTTP}" = "1" ]; then
        check_http_listener || true
    fi

    report
}

main "$@"