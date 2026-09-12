#!/usr/bin/env bash
# scripts/check-no-env-staged.sh
#
# Pre-commit guard: reject staging of `.env` files.
# Allows `.env.example`, `.env.test`, `.env.local`, and any sibling whose
# name is NOT exactly `.env` (per secure-configuration Requirement
# "`.env` Is Never Tracked", Scenario "accidental staging is rejected").
#
# Used as the `entry` of the local pre-commit hook defined in
# `.pre-commit-config.yaml`. Reads staged paths from `git diff --cached`
# (the staged index) and exits 1 if any path matches `^\.env$` or
# `^.*/\.env$`. The `--from-stdin` mode is used by the test suite to
# exercise the regex without a real staged index.

set -euo pipefail

if [ "${1:-}" = "--from-stdin" ]; then
    staged=$(cat)
else
    staged=$(git diff --cached --name-only --diff-filter=ACMRT 2>/dev/null || true)
fi

# `^(\.env|.*/\.env)$`:
#   - `.env`            matches the first alternative
#   - `path/to/.env`    matches the second alternative
#   - `.env.example`    does NOT match (the trailing `.example` fails `$`)
#   - `.envrc`          does NOT match (the trailing `rc` fails `$`)
violations=$(printf '%s\n' "$staged" | grep -E '^(\.env|.*/\.env)$' || true)

if [ -n "$violations" ]; then
    echo "ERROR: .env file staged — use .env.example instead" >&2
    echo "$violations" >&2
    exit 1
fi

exit 0
