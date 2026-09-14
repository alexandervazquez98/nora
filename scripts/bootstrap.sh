#!/usr/bin/env bash
# scripts/bootstrap.sh
#
# One-line wrapper around `scripts/install.sh`. Clones the NORA repo into
# a deterministic temp directory, invokes install.sh from that clone, then
# cleans up — unless the operator asked for `--keep-clone` or
# `--download-only --dest <path>`.
#
# bootstrap.sh is a WRAPPER, not a refactor of install.sh. The deploy
# contract remains scripts/install.sh; this script only automates the
# "bring the source tree" phase that the operator would otherwise have to
# do by hand.
#
# Recommended flow (preserved as the default in INSTALL.md):
#   git clone https://github.com/alexandervazquez98/nora.git
#   cd nora
#   sudo scripts/install.sh
#
# Convenience flow (this script, for advanced operators / IaC):
#   curl -fsSL https://raw.githubusercontent.com/alexandervazquez98/nora/main/scripts/bootstrap.sh | sudo bash -s --
#
# Pin a ref:
#   curl -fsSL .../bootstrap.sh | sudo bash -s -- --ref v0.2.0
#
# Point at an internal mirror (air-gapped):
#   curl -fsSL https://internal-mirror.example.com/nora/scripts/bootstrap.sh | sudo bash -s -- \
#       --repo https://internal-mirror.example.com/nora.git
#
# Download only, audit before install:
#   curl -fsSL .../bootstrap.sh | sudo bash -s -- --download-only --dest /tmp/nora-review
#
# Security posture (intentional, not negotiable via flags):
#   * Repo URL MUST start with https://. Cleartext git:// / http:// is
#     refused before any network I/O so credentials never travel in the
#     clear and a hostile redirect cannot downgrade the transport.
#   * Ref MUST NOT contain shell-injection vectors
#     (--upload-pack=, ext::, ;, |, $, `, backslash, leading -). Those
#     would let a malicious ref name execute commands during `git clone`
#     or `git pull`.
#   * Running as root with empty SUDO_USER is REFUSED. That pattern means
#     the operator piped curl into bash as direct root (no `sudo` shell),
#     which leaves no audit trail of the real user and prevents the
#     standard "git clone as SUDO_USER so .git ownership is sane" pattern.
#   * The cloned commit SHA is printed before install.sh runs AND verified
#     after, so the operator sees exactly what landed in /opt/nora.
#
# See INSTALL.md § "One-line bootstrap" for the operator-facing docs.

set -euo pipefail
IFS=$'\n\t'

# ---------------------------------------------------------------------------
# Constants — defaults overridable via flags.
# ---------------------------------------------------------------------------
readonly DEFAULT_REPO="https://github.com/alexandervazquez98/nora.git"
readonly DEFAULT_REF="main"
readonly DEFAULT_PREFIX="/opt/nora"
readonly DEFAULT_CONFIG_DIR="/etc/nora"
readonly TMP_PREFIX="/tmp/nora-bootstrap"
readonly INSTALL_SCRIPT_REL="scripts/install.sh"

# ---------------------------------------------------------------------------
# Mutable state — set by parse_args.
# ---------------------------------------------------------------------------
REPO="${DEFAULT_REPO}"
REF="${DEFAULT_REF}"
PREFIX="${DEFAULT_PREFIX}"
CONFIG_DIR="${DEFAULT_CONFIG_DIR}"
DEST=""
DOWNLOAD_ONLY="false"
KEEP_CLONE="false"
FORCE_ENV_FILE="false"
FORCE_TRANSPORT_ENV="false"

# Set by main() — used by the cleanup trap so it knows whether to keep
# the temp directory around on failure.
TMP_DIR=""

# ---------------------------------------------------------------------------
# Logging — stderr for humans, stdout reserved for future machine use.
# ---------------------------------------------------------------------------
log()  { printf '[OK] %s\n'   "$*" >&2; }
warn() { printf '[WARN] %s\n' "$*" >&2; }
fail() { printf '[FAIL] %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<EOF
Usage: bootstrap.sh [OPTIONS]

Clone the NORA repo and (optionally) invoke scripts/install.sh from the clone.

Options:
  --ref REF             Branch, tag, or commit SHA to clone (default: ${DEFAULT_REF}).
  --repo URL            Git URL of the NORA repo (default: ${DEFAULT_REPO}).
                        MUST start with https:// — non-HTTPS URLs are refused.
  --prefix PATH         Forwarded to install.sh --prefix (default: ${DEFAULT_PREFIX}).
  --config-dir PATH     Forwarded to install.sh --config-dir (default: ${DEFAULT_CONFIG_DIR}).
  --download-only       Clone only; do not invoke install.sh. Use --dest to choose where.
  --dest PATH           Destination directory for --download-only (default: cwd).
                        Ignored unless --download-only is set.
  --keep-clone          Do not delete the temporary clone after install.
                        Useful for debugging a failed install.
  --yes                 Pass --force-env-file to install.sh if a previous nora.env differs.
  --force-transport-env Pass --force-transport-env to install.sh (re-materialize nora-mcp.env).
  --help, -h            Show this help and exit.

Exit codes:
  0  success (clone + install OK, or --download-only OK).
  1  bootstrap failure (validation, clone, or install.sh error).
  2  bad arguments.

Security:
  HTTPS-only repo URL. Refs containing --upload-pack=, ext::, ;, |, \$, \`,
  backslash, or starting with '-' are refused before any network I/O.
  Running as root with empty SUDO_USER is refused (use 'sudo bash', not direct 'bash').
EOF
}

# ---------------------------------------------------------------------------
# Argument parsing — fail loud on unknown flags (exit 2 to match install.sh).
# ---------------------------------------------------------------------------
parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --ref)           REF="$2"; shift 2 ;;
            --repo)          REPO="$2"; shift 2 ;;
            --prefix)        PREFIX="$2"; shift 2 ;;
            --config-dir)    CONFIG_DIR="$2"; shift 2 ;;
            --download-only) DOWNLOAD_ONLY="true"; shift ;;
            --dest)          DEST="$2"; shift 2 ;;
            --keep-clone)    KEEP_CLONE="true"; shift ;;
            --yes)           FORCE_ENV_FILE="true"; shift ;;
            --force-transport-env) FORCE_TRANSPORT_ENV="true"; shift ;;
            --help|-h)       usage; exit 0 ;;
            --)              shift; break ;;
            -*)
                printf '[FAIL] Unknown argument: %s. Try --help.\n' "$1" >&2
                exit 2
                ;;
            *)
                printf '[FAIL] Unexpected positional argument: %s. Try --help.\n' "$1" >&2
                exit 2
                ;;
        esac
    done
}

# ---------------------------------------------------------------------------
# Helper — REF contains shell-injection vector or whitespace.
# Pulled out so shellcheck doesn't have to lint a single case pattern
# with mixed single-quoted metacharacters (which trips SC1003 even
# though the pattern is intentional). The list mirrors the git
# project's own recommendations for safe ref names plus shell
# metacharacters that would be re-interpreted if a future refactor
# pipes $REF into `eval` or a sub-shell.
# ---------------------------------------------------------------------------
ref_contains_unsafe() {
    local s="$1"
    [[ "${s}" == *"--upload-pack="* ]] && return 0
    [[ "${s}" == *"ext::"* ]] && return 0
    [[ "${s}" == *";"* ]] && return 0
    [[ "${s}" == *"|"* ]] && return 0
    [[ "${s}" == *'$'* ]] && return 0
    [[ "${s}" == *'`'* ]] && return 0
    [[ "${s}" == *\\* ]] && return 0
    [[ "${s}" == *" "* ]] && return 0
    return 1
}

# ---------------------------------------------------------------------------
# Security validations — refuse dangerous inputs before any network I/O.
# ---------------------------------------------------------------------------
validate_inputs() {
    # HTTPS-only on REPO. We check the prefix only — full URL validation
    # (host existence, etc.) is left to `git clone` itself, which gives a
    # clearer error than we could synthesise here.
    #
    # Test-only escape hatch: BOOTSTRAP_TEST_ALLOW_NON_HTTPS=1 skips this
    # check so the pytest suite can exercise the clone + install flow
    # against a local file:// remote without standing up an HTTPS server.
    # The variable name starts with BOOTSTRAP_TEST_ so it cannot be set
    # accidentally by a wrapper or systemd unit, and it is intentionally
    # undocumented in --help because no production operator should
    # ever set it.
    if [[ "${BOOTSTRAP_TEST_ALLOW_NON_HTTPS:-0}" != "1" ]]; then
        if [[ "${REPO}" != https://* ]]; then
            fail "Refusing non-HTTPS repo URL: ${REPO} (HTTPS required to keep credentials off the wire)."
        fi
    fi

    # REF must not contain shell-injection vectors. The list mirrors the
    # git project's own recommendations for safe ref names plus the
    # shell metacharacters that would be re-interpreted if a future
    # refactor pipes $REF into `eval` or a sub-shell.
    if ref_contains_unsafe "${REF}"; then
        fail "Refusing suspicious ref: ${REF} (contains shell-injection vector or whitespace)."
    fi
    # A leading '-' would be parsed as a flag by `git checkout`/`git clone`.
    if [[ "${REF}" == -* ]]; then
        fail "Refusing ref starting with '-': ${REF} (would be parsed as git flag)."
    fi

    # Running as root with empty SUDO_USER means someone did
    # `curl ... | bash` directly as root (no sudo shell). That's the
    # pattern we refuse: there is no original user identity to drop to
    # for git clone, and there is no audit trail.
    if [[ "${EUID}" -eq 0 && -z "${SUDO_USER:-}" ]]; then
        fail "Running as root with empty SUDO_USER. Use 'sudo bash -s -- ...' not direct 'bash', so git clone inherits your identity."
    fi

    # --dest is meaningful only with --download-only.
    if [[ "${DOWNLOAD_ONLY}" != "true" && -n "${DEST}" ]]; then
        fail "--dest is only valid with --download-only."
    fi
}

# ---------------------------------------------------------------------------
# Clone (or pull) — resolves the working tree the rest of the script uses.
#
# Sets the global CLONE_DIR to a real path. Returns the captured HEAD SHA
# via stdout so callers can capture it without sub-shell quoting hazards.
# ---------------------------------------------------------------------------
clone_or_pull() {
    local target_dir="$1"

    if [[ -d "${target_dir}/.git" ]]; then
        log "Existing clone at ${target_dir}; running git pull --ff-only."
        # SUDO_USER is set: drop privileges so the pull also runs as the
        # original user. SUDO_USER is empty: we are not root (EUID != 0
        # case), so run as the current user.
        if [[ "${EUID}" -eq 0 && -n "${SUDO_USER:-}" ]]; then
            sudo -u "${SUDO_USER}" git -C "${target_dir}" pull --ff-only \
                || fail "git pull --ff-only failed in ${target_dir}; remove the directory or pass a different --dest."
        else
            git -C "${target_dir}" pull --ff-only \
                || fail "git pull --ff-only failed in ${target_dir}; remove the directory or pass a different --dest."
        fi
    else
        log "Cloning ${REPO} (ref=${REF}) into ${target_dir}..."
        if [[ "${EUID}" -eq 0 && -n "${SUDO_USER:-}" ]]; then
            sudo -u "${SUDO_USER}" git clone --branch "${REF}" --depth 1 "${REPO}" "${target_dir}" \
                || fail "git clone failed for ${REPO} ref=${REF}."
        else
            git clone --branch "${REF}" --depth 1 "${REPO}" "${target_dir}" \
                || fail "git clone failed for ${REPO} ref=${REF}."
        fi
    fi

    # Capture the HEAD SHA so the operator can pin what they got even
    # after the temp dir is cleaned up. Use `git rev-parse` not `git log`
    # so we don't depend on depth=1 visibility (a fresh clone with
    # depth=1 has the SHA reachable either way; defensive for the
    # pull path where depth might be larger).
    git -C "${target_dir}" rev-parse HEAD
}

# ---------------------------------------------------------------------------
# Run install.sh — wrapper that forwards flags and preserves the failure
# message so the temp clone survives a failure for inspection.
# ---------------------------------------------------------------------------
run_install() {
    local install_script="$1"

    local -a install_args=(
        --prefix "${PREFIX}"
        --config-dir "${CONFIG_DIR}"
    )
    if [[ "${FORCE_ENV_FILE}" == "true" ]]; then
        install_args+=(--force-env-file)
    fi
    if [[ "${FORCE_TRANSPORT_ENV}" == "true" ]]; then
        install_args+=(--force-transport-env)
    fi

    log "Invoking ${install_script} ${install_args[*]}..."
    # We invoke install.sh directly (no `sudo` here): bootstrap.sh is
    # expected to be run under sudo already, and install.sh's own prereq
    # check fails fast if the operator forgot. Re-sudoing would only
    # confuse the audit trail.
    if ! bash "${install_script}" "${install_args[@]}"; then
        local rc=$?
        fail "install.sh failed (exit ${rc}). Clone preserved at: ${TMP_DIR}/nora for inspection. Re-run with --keep-clone to retain permanently."
    fi
}

# ---------------------------------------------------------------------------
# Post-install verification — if /opt/nora is a git working tree, confirm
# its HEAD matches what we cloned. This catches the case where install.sh
# did `cp -a` from a stale source.
# ---------------------------------------------------------------------------
verify_installed_sha() {
    local expected_sha="$1"
    local install_root="$2"

    if [[ ! -d "${install_root}/.git" ]]; then
        # install.sh may copy files without preserving .git; nothing to
        # verify in that case. Log so the operator sees why we skipped.
        log "Install root ${install_root} has no .git; skipping SHA verification."
        return 0
    fi

    local installed_sha
    installed_sha="$(git -C "${install_root}" rev-parse HEAD)" \
        || { warn "Could not read installed SHA from ${install_root}."; return 0; }

    if [[ "${installed_sha}" != "${expected_sha}" ]]; then
        warn "Installed SHA ${installed_sha} differs from cloned SHA ${expected_sha}."
    else
        log "Installed SHA matches clone: ${expected_sha}"
    fi
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
main() {
    parse_args "$@"
    validate_inputs

    local clone_dir clone_sha install_script

    if [[ "${DOWNLOAD_ONLY}" == "true" ]]; then
        # --download-only: clone to <dest>/nora, never invoke install.sh.
        : "${DEST:=$(pwd)}"
        clone_dir="${DEST%/}/nora"
        if [[ -e "${clone_dir}" && ! -d "${clone_dir}/.git" && -n "$(ls -A "${clone_dir}" 2>/dev/null)" ]]; then
            fail "Destination ${clone_dir} exists and is non-empty. Refusing to overwrite."
        fi
        mkdir -p "${DEST}"
        clone_sha="$(clone_or_pull "${clone_dir}")"
        log "Download complete (${clone_sha}). Inspect at: ${clone_dir}"
        exit 0
    fi

    # Full install: temp clone + install.sh + cleanup-on-success.
    TMP_DIR="${TMP_PREFIX}-$(date +%s)"
    clone_dir="${TMP_DIR}/nora"
    mkdir -p "${TMP_DIR}"

    clone_sha="$(clone_or_pull "${clone_dir}")"
    log "Cloned commit: ${clone_sha}"

    install_script="${clone_dir}/${INSTALL_SCRIPT_REL}"
    if [[ ! -x "${install_script}" ]]; then
        fail "Expected ${install_script} to be executable; not found. Wrong ref? Wrong repo?"
    fi

    run_install "${install_script}"
    verify_installed_sha "${clone_sha}" "${PREFIX}"

    log "Bootstrap complete. NORA installed to ${PREFIX} (commit ${clone_sha})."
}

# ---------------------------------------------------------------------------
# Cleanup trap — runs on EXIT (success, failure, signal). On success with
# a full install, remove the temp clone unless --keep-clone was passed.
# On failure, ALWAYS keep the clone (the failure message above names
# the path so the operator knows where to look).
# ---------------------------------------------------------------------------
cleanup() {
    local rc=$?

    # If we never reached main() (e.g. parse_args failed), TMP_DIR is
    # empty — nothing to do.
    if [[ -z "${TMP_DIR}" ]]; then
        exit "${rc}"
    fi

    # On success with a non-download-only install, remove the temp clone
    # unless --keep-clone was set. Failure paths leave it in place — the
    # run_install / clone_or_pull helpers already printed the path.
    if [[ "${rc}" -eq 0 && "${DOWNLOAD_ONLY}" != "true" && "${KEEP_CLONE}" != "true" && -d "${TMP_DIR}" ]]; then
        rm -rf "${TMP_DIR}"
    fi

    exit "${rc}"
}

trap cleanup EXIT
main "$@"