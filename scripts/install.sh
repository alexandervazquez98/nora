#!/usr/bin/env bash
# scripts/install.sh
#
# Install the NORA MCP server onto a fresh Linux host.
#
# Automates the 5-step manual procedure documented in INSTALL.md:
#   1. Clone the repo and `uv sync`                       -> phase_install
#   2. Generate the catalog signing key                   -> phase_signing_key
#   3. Materialize /etc/nora/nora.env from .env.example    -> phase_env_file
#   4. Re-sign the OID catalogs                           -> phase_catalog
#   5. Install + enable the systemd unit                  -> phase_systemd
#
# Plus three supporting phases:
#   - phase_prereq : python3 / uv / git / systemctl are present + meet min versions
#   - phase_user   : create the unprivileged service account
#   - phase_dirs   : mkdir the install / config / log / state directories
#   - phase_summary: print a green [OK] block with paths + masked key preview
#
# Usage:
#   sudo scripts/install.sh                            # real install, defaults
#   sudo scripts/install.sh --dry-run                  # print every step, change nothing
#   sudo scripts/install.sh --skip-systemd             # don't touch systemd
#   sudo scripts/install.sh --skip-signing-key         # don't regenerate the key
#   sudo scripts/install.sh --skip-catalog             # don't re-sign catalogs
#   sudo scripts/install.sh --force-env-file           # overwrite /etc/nora/nora.env
#   scripts/install.sh --help                          # this message
#
# Every phase is idempotent: re-running on an already-installed system is
# a sequence of [SKIP] / [OK] lines, never a failure. The script is
# security-first: the signing key is never echoed, only the masked
# `ab...yz` form appears in operator-facing output.
#
# See INSTALL.md for the manual install procedure this script automates.
# Exit codes: 0 success, 1 phase failure, 2 bad arguments, 3 prereq missing.

set -euo pipefail
IFS=$'\n\t'
# ERR trap — fires on the first failing command (with `set -e` the script
# then exits with that command's exit code). We never change the exit
# code, only annotate it.
trap 'printf "%s failed at line %s\n" "${SCRIPT_NAME:-install.sh}" "${LINENO}" >&2' ERR

# ---------------------------------------------------------------------------
# Constants — self-locate the script and the repo root.
# ---------------------------------------------------------------------------
SCRIPT_PATH="${BASH_SOURCE[0]}"
# `readlink -f` is GNU; fall back to `realpath` if missing (macOS).
if command -v readlink >/dev/null 2>&1 && readlink -f "${SCRIPT_PATH}" >/dev/null 2>&1; then
    SCRIPT_PATH="$(readlink -f "${SCRIPT_PATH}")"
elif command -v realpath >/dev/null 2>&1; then
    SCRIPT_PATH="$(realpath "${SCRIPT_PATH}")"
fi
SCRIPT_DIR="$(dirname "${SCRIPT_PATH}")"
SCRIPT_NAME="$(basename "${SCRIPT_PATH}")"
# REPO_ROOT — the directory that *contains* this `scripts/` dir. Used by
# phase_install to copy the repo onto a fresh PREFIX.
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

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

DRY_RUN=0
SKIP_SIGNING_KEY=0
SKIP_SYSTEMD=0
SKIP_CATALOG=0
FORCE_ENV_FILE=0
# NO_COLOR is honored as a courtesy: future color output (e.g., a colored
# phase banner) would gate on `[ "${NO_COLOR}" = "0" ] && [ -t 1 ]`. The
# current implementation is colorless, so the variable is parsed but not
# read at the moment — that is intentional, not a stale declaration.
NO_COLOR="${NO_COLOR:-0}"
export NO_COLOR

# ---------------------------------------------------------------------------
# Logging helpers — log to stderr; stdout stays clean for machine consumers.
# ---------------------------------------------------------------------------

log() {
    local msg="$1"
    printf '[OK] %s\n' "${msg}" >&2
}

warn() {
    local msg="$1"
    printf '[WARN] %s\n' "${msg}" >&2
}

fail() {
    local msg="$1"
    printf '[FAIL] %s\n' "${msg}" >&2
}

# run <description> <command-string...>
# Execute the command. In dry-run, prints [DRY-RUN] and skips execution.
# NEVER echoes the command args — args may contain the signing key.
run() {
    local desc="$1"
    shift
    if [ "${DRY_RUN}" = "1" ]; then
        printf '[DRY-RUN] %s\n' "${desc}" >&2
        return 0
    fi
    eval "$@"
    local rc=$?
    if [ "${rc}" -eq 0 ]; then
        printf '[OK] %s\n' "${desc}" >&2
    else
        printf '[FAIL] %s (rc=%d)\n' "${desc}" "${rc}" >&2
    fi
    return "${rc}"
}

# mask_key <key>
# Print the key as `ab...yz` (first 2 + last 2) so the key never lands in
# operator-facing output. Call this any time the key needs to appear in
# a log line. Internal use only — never pipe a real key through printf.
mask_key() {
    local key="$1"
    local len="${#key}"
    if [ "${len}" -lt 4 ]; then
        printf '<<too-short:%d>>' "${len}"
    elif [ "${len}" -lt 8 ]; then
        printf '%s...%s' "${key:0:2}" "${key: -2}"
    else
        printf '%s...%s' "${key:0:4}" "${key: -4}"
    fi
}

# ---------------------------------------------------------------------------
# usage + parse_args
# ---------------------------------------------------------------------------

usage() {
    cat <<'USAGE' >&2
Usage: sudo scripts/install.sh [options]

Automates the 5-step install procedure in INSTALL.md. Re-run safely.

Options:
  --dry-run               Print every step without changing the filesystem.
  --user NAME             Service-account username (default: nora).
  --prefix PATH           Install prefix (default: /opt/nora).
  --config-dir PATH       Runtime config directory (default: /etc/nora).
  --state-dir PATH        Mutable state directory (default: /var/lib/nora).
  --log-dir PATH          Log directory (default: /var/log/nora).
  --skip-signing-key      Don't regenerate the catalog signing key.
  --skip-systemd          Don't install / enable the systemd unit.
  --skip-catalog          Don't re-sign the OID catalogs.
  --force-env-file        Overwrite /etc/nora/nora.env from .env.example.
  --no-color              Reserved for future ANSI color output.
  --help                  Show this help and exit 0.

Exit codes:
  0  success
  1  phase failure
  2  bad arguments
  3  prereq missing
USAGE
}

parse_args() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --dry-run) DRY_RUN=1 ;;
            --user)
                [ "$#" -ge 2 ] || { fail "--user requires an argument"; exit 2; }
                USER_NAME="$2"; shift
                ;;
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
            --skip-signing-key) SKIP_SIGNING_KEY=1 ;;
            --skip-systemd) SKIP_SYSTEMD=1 ;;
            --skip-catalog) SKIP_CATALOG=1 ;;
            --force-env-file) FORCE_ENV_FILE=1 ;;
            --no-color) NO_COLOR=1 ;;
            --help|-h) usage; exit 0 ;;
            --) shift; break ;;
            -*) fail "unknown flag: $1"; usage; exit 2 ;;
            *) fail "unexpected positional argument: $1"; usage; exit 2 ;;
        esac
        shift
    done
}

# ---------------------------------------------------------------------------
# Phase implementations — one function per INSTALL.md step plus prereqs.
# Each phase is independently invokable and idempotent. Returns 0 on
# success, non-zero on failure.
# ---------------------------------------------------------------------------

phase_prereq() {
    local min_py_major=3
    local min_py_minor=12

    # python3 — required, with explicit min version check via the
    # python3 interpreter itself (no awk/sed regex over version strings).
    if ! command -v python3 >/dev/null 2>&1; then
        fail "python3 not found on PATH (need >= ${min_py_major}.${min_py_minor})"
        return 1
    fi
    if ! python3 -c "import sys; sys.exit(0 if sys.version_info >= (${min_py_major}, ${min_py_minor}) else 1)" 2>/dev/null; then
        local ver
        ver="$(python3 --version 2>&1 || true)"
        fail "python3 too old: ${ver} (need >= ${min_py_major}.${min_py_minor})"
        return 1
    fi

    # uv — required.
    if ! command -v uv >/dev/null 2>&1; then
        fail "uv not found on PATH (need >= 0.4). Install with: pip install uv"
        return 1
    fi

    # git — required.
    if ! command -v git >/dev/null 2>&1; then
        fail "git not found on PATH (need >= 2.30)"
        return 1
    fi

    # systemctl — required unless --skip-systemd.
    if [ "${SKIP_SYSTEMD}" != "1" ]; then
        if ! command -v systemctl >/dev/null 2>&1; then
            fail "systemctl not found on PATH (need >= 250). Use --skip-systemd to install without it."
            return 1
        fi
    fi

    log "prerequisites present (python3, uv, git$( [ "${SKIP_SYSTEMD}" = "1" ] && printf '' || printf ', systemctl'))"
    return 0
}

phase_user() {
    # Idempotent: skip if user already exists. The user is created with
    # `--system` so no password / aging, with a nologin shell so interactive
    # login is impossible. Home dir points at the state dir so any cached
    # state lands in a writable place.
    if id -u "${USER_NAME}" >/dev/null 2>&1; then
        printf '[SKIP] user %s already exists\n' "${USER_NAME}" >&2
        return 0
    fi
    run "create user ${USER_NAME}" \
        "useradd --system --shell '/usr/sbin/nologin' --home-dir '${STATE_DIR}' '${USER_NAME}'"
}

phase_dirs() {
    # PREFIX may already exist (operator pre-created); mkdir -p is fine.
    run "mkdir ${PREFIX}" "mkdir -p '${PREFIX}'"
    # CONFIG_DIR, LOG_DIR, STATE_DIR — owned by nora:nora. Sticky (1777)
    # on interventions so OpenChat (a different uid) can write to the
    # shared dir while preserving the nora-owned files.
    run "mkdir ${CONFIG_DIR}" \
        "install -d -m 0750 -o '${USER_NAME}' -g '${USER_NAME}' '${CONFIG_DIR}'"
    run "mkdir ${LOG_DIR}" \
        "install -d -m 0750 -o '${USER_NAME}' -g '${USER_NAME}' '${LOG_DIR}'"
    run "mkdir ${STATE_DIR}" \
        "install -d -m 0750 -o '${USER_NAME}' -g '${USER_NAME}' '${STATE_DIR}'"
    run "mkdir ${STATE_DIR}/interventions (1777)" \
        "install -d -m 1777 -o '${USER_NAME}' -g '${USER_NAME}' '${STATE_DIR}/interventions'"
}

phase_install() {
    # Fresh install: copy the repo, then sync deps. Update: git pull +
    # uv sync. The `.venv/bin/nora-mcp` probe is the freshness signal.
    if [ ! -x "${PREFIX}/.venv/bin/nora-mcp" ]; then
        run "copy repo to ${PREFIX}" "cp -a '${REPO_ROOT}/.' '${PREFIX}/'"
        run "uv sync in ${PREFIX}" "cd '${PREFIX}' && uv sync"
    else
        run "git pull --ff-only in ${PREFIX}" "cd '${PREFIX}' && git pull --ff-only"
        run "uv sync in ${PREFIX}" "cd '${PREFIX}' && uv sync"
    fi
}

phase_signing_key() {
    # Generate a fresh key with `secrets.token_urlsafe(32)` and install it
    # directly into /etc/nora/signing_key with mode 0600 owned by nora:nora.
    # We pipe directly into `install` so the key is never written to a
    # world-readable tempfile.
    local key_file="${CONFIG_DIR}/signing_key"

    if [ ! -f "${key_file}" ]; then
        run "generate signing key" \
            "${PREFIX}/.venv/bin/python '${PREFIX}/scripts/generate_signing_key.py' | install -m 0600 -o '${USER_NAME}' -g '${USER_NAME}' /dev/stdin '${key_file}'"
        return 0
    fi

    # Existing key — validate mode + owner. FAIL loud if either is wrong.
    local mode owner
    if stat -c '%a' "${key_file}" >/dev/null 2>&1; then
        mode="$(stat -c '%a' "${key_file}")"
        owner="$(stat -c '%U:%G' "${key_file}")"
    else
        # macOS stat (BSD) — only hit on the test env, never on real installs.
        mode="$(stat -f '%Lp' "${key_file}")"
        owner="$(stat -f '%Su:%Sg' "${key_file}")"
    fi
    if [ "${mode}" != "600" ]; then
        fail "signing_key has mode ${mode}, expected 600 — fix with: sudo chmod 0600 ${key_file}"
        return 1
    fi
    if [ "${owner}" != "${USER_NAME}:${USER_NAME}" ]; then
        fail "signing_key owned by ${owner}, expected ${USER_NAME}:${USER_NAME} — fix with: sudo chown ${USER_NAME}:${USER_NAME} ${key_file}"
        return 1
    fi
    printf '[SKIP] signing_key already present (mode=%s owner=%s)\n' "${mode}" "${owner}" >&2
    return 0
}

phase_env_file() {
    # Materialize /etc/nora/nora.env from .env.example. Re-runs skip this
    # unless --force-env-file. Inject the freshly generated signing key
    # on new installs and on --force-env-file so the runtime never boots
    # with a stale key.
    local env_file="${CONFIG_DIR}/nora.env"
    local example="${PREFIX}/.env.example"

    local should_copy=0
    if [ ! -f "${env_file}" ]; then
        should_copy=1
    elif [ "${FORCE_ENV_FILE}" = "1" ]; then
        should_copy=1
    fi

    if [ "${should_copy}" = "1" ]; then
        run "materialize ${env_file} from .env.example" \
            "install -m 0640 -o '${USER_NAME}' -g '${USER_NAME}' '${example}' '${env_file}'"
    else
        printf '[SKIP] %s already exists (use --force-env-file to overwrite)\n' "${env_file}" >&2
    fi

    # Inject the signing key into the env file in-place. Only do this
    # when we just (re-)created the file — never on a no-op re-run where
    # the operator may have rotated the key by hand (per OPERATIONS.md).
    if [ -f "${CONFIG_DIR}/signing_key" ] && [ "${should_copy}" = "1" ]; then
        # Read the key into a local var. We do NOT echo it — it is only
        # ever passed to `sed -i` below as a substitution value.
        local key
        key="$(cat "${CONFIG_DIR}/signing_key")"
        run "inject NORA_OID_CATALOG_SIGNING_KEY into ${env_file}" \
            "sed -i 's|^NORA_OID_CATALOG_SIGNING_KEY=.*|NORA_OID_CATALOG_SIGNING_KEY=${key}|' '${env_file}'"
    fi
}

phase_catalog() {
    # Re-sign every catalog under ${PREFIX}/data/oid-catalogs/.
    # sign_catalog.py reads NORA_OID_CATALOG_SIGNING_KEY from the env file;
    # we re-source the env file (after phase_env_file materialized it)
    # to inherit the key in this shell.
    if [ -f "${CONFIG_DIR}/nora.env" ]; then
        # `set -a` exports every variable assignment from the sourced file
        # so it becomes part of this process's environment. Disable the
        # lint warning about dynamic sourcing — the file path is local
        # and operator-controlled.
        # shellcheck disable=SC1090,SC1091
        set -a
        # shellcheck disable=SC1090,SC1091
        . "${CONFIG_DIR}/nora.env"
        set +a
    fi

    if [ ! -d "${PREFIX}/data/oid-catalogs" ]; then
        warn "no catalogs dir at ${PREFIX}/data/oid-catalogs; skipping re-sign"
        return 0
    fi

    local catalog
    # `find ... -print0 | while IFS= read -r -d ''` is the safe form for
    # arbitrary filenames; we use plain `find ... | while read -r` here
    # because catalog paths are constrained (vendor/model/firmware.json)
    # and the failure mode (space in path) is benign — the runner will
    # just fail to find that file.
    #
    # Each catalog lives at <output_root>/<vendor>/<model>/<firmware>.json.
    # We MUST pass --vendor / --model / --firmware so sign_catalog.py
    # writes the correct envelope; without them it defaults to
    # cambium/pmp450i/15.2.1 and every iteration of this loop re-signs
    # the SAME file, leaving the other firmware (e.g. 15.3.0.json)
    # unsigned. That unsigned catalog then fails HMAC verification at
    # boot with `CatalogVerificationError: signature mismatch`. Closes
    # #32.
    while IFS= read -r catalog; do
        [ -z "${catalog}" ] && continue
        local firmware vendor model
        firmware="$(basename "${catalog}" .json)"
        model="$(basename "$(dirname "${catalog}")")"
        vendor="$(basename "$(dirname "$(dirname "${catalog}")")")"
        run "sign ${vendor}/${model}/${firmware}.json" \
            "cd '${PREFIX}' && NORA_OID_CATALOG_SIGNING_KEY=\"\${NORA_OID_CATALOG_SIGNING_KEY:-}\" '${PREFIX}/.venv/bin/python' '${PREFIX}/scripts/sign_catalog.py' --vendor '${vendor}' --model '${model}' --firmware '${firmware}' --output-root '${PREFIX}/data/oid-catalogs'"
    done < <(find "${PREFIX}/data/oid-catalogs" -name '*.json' -type f)
}

phase_systemd() {
    run "install nora-mcp.service unit" \
        "install -m 0644 '${PREFIX}/scripts/nora-mcp.service' '/etc/systemd/system/nora-mcp.service'"
    run "systemctl daemon-reload" "systemctl daemon-reload"
    run "systemctl enable --now nora-mcp" "systemctl enable --now nora-mcp"
}

phase_summary() {
    # Emit a human-readable summary of the install shape. The signing key
    # appears ONLY in the ab...yz truncated form — never the raw value.
    local key_file="${CONFIG_DIR}/signing_key"
    local masked="<<unset>>"
    if [ -f "${key_file}" ]; then
        masked="$(mask_key "$(cat "${key_file}")")"
    fi

    cat >&2 <<SUMMARY

[OK] NORA install complete.

  Install prefix : ${PREFIX}
  Config dir     : ${CONFIG_DIR}
  State dir      : ${STATE_DIR}
  Log dir        : ${LOG_DIR}
  Service user   : ${USER_NAME}
  Signing key    : ${masked}

  Next step: sudo scripts/verify-install.sh
SUMMARY
}

# ---------------------------------------------------------------------------
# main — orchestrate phases in order.
# ---------------------------------------------------------------------------

main() {
    parse_args "$@"

    phase_prereq      || { fail "phase_prereq failed"; exit 3; }
    phase_user        || { fail "phase_user failed"; exit 1; }
    phase_dirs        || { fail "phase_dirs failed"; exit 1; }
    phase_install     || { fail "phase_install failed"; exit 1; }

    if [ "${SKIP_SIGNING_KEY}" = "1" ]; then
        printf '[SKIP] --skip-signing-key: phase_signing_key bypassed\n' >&2
    else
        phase_signing_key || { fail "phase_signing_key failed"; exit 1; }
    fi

    phase_env_file    || { fail "phase_env_file failed"; exit 1; }

    if [ "${SKIP_CATALOG}" = "1" ]; then
        printf '[SKIP] --skip-catalog: phase_catalog bypassed\n' >&2
    else
        phase_catalog   || { fail "phase_catalog failed"; exit 1; }
    fi

    if [ "${SKIP_SYSTEMD}" = "1" ]; then
        printf '[SKIP] --skip-systemd: phase_systemd bypassed\n' >&2
    else
        phase_systemd   || { fail "phase_systemd failed"; exit 1; }
    fi

    phase_summary
    return 0
}

main "$@"