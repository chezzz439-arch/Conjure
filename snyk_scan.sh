#!/usr/bin/env bash
#
# Run the full Snyk scan over the kiosk: dependencies (SCA) + source code (SAST).
#
#   ./snyk_scan.sh
#
# Needs SNYK_TOKEN, either exported or set in .env. Get one from
# https://app.snyk.io/account (it is the "auth token" field), then either:
#
#   echo 'SNYK_TOKEN=...' >> .env      # preferred, matches every other key here
#   export SNYK_TOKEN=...              # one-off
#
# This script only ever *reports*. It never runs `snyk fix`, never edits
# requirements.txt, and never touches main.py — fixing a transitive pin can
# quietly change slicer or mesh behaviour, so that stays a human decision.
#
# Two things about this project that the defaults get wrong, both found the hard
# way rather than assumed:
#
#  1. Snyk's pip plugin spawns `python`, not `python3`. This Mac has no bare
#     `python` on PATH, so a plain `snyk test` dies with `spawn python ENOENT`
#     reported as the much less helpful "Failed to test pip project". Hence
#     --command below. It also has to point at the venv rather than any old
#     interpreter, because that is where the deps are actually installed.
#
#  2. Snyk exits 1 when it finds vulnerabilities. That is a successful scan, not
#     a broken one, so `set -e` would abort exactly when the scan worked.

set -uo pipefail
cd "$(dirname "$0")"

VENV_PY="$PWD/.venv/bin/python"
OUT_DIR="$PWD/output/snyk"          # under output/, which is gitignored
mkdir -p "$OUT_DIR"

# ── token ────────────────────────────────────────────────────────────────────
if [ -z "${SNYK_TOKEN:-}" ] && [ -f .env ]; then
    # Only pull this one key out of .env; sourcing the whole file would export
    # a dozen live API keys into the shell as a side effect of a security scan.
    SNYK_TOKEN=$(grep -E '^\s*SNYK_TOKEN\s*=' .env | tail -1 | cut -d= -f2- \
                 | tr -d '"'"'"' \r' )
fi

if [ -z "${SNYK_TOKEN:-}" ]; then
    echo "SNYK_TOKEN is not set."
    echo "  Add it to .env:  SNYK_TOKEN=<token from https://app.snyk.io/account>"
    echo "  or export it:    export SNYK_TOKEN=..."
    exit 1
fi
export SNYK_TOKEN

if [ ! -x "$VENV_PY" ]; then
    echo "No interpreter at $VENV_PY — Snyk cannot resolve requirements.txt"
    echo "  without one, and the system has no bare \`python\`."
    exit 1
fi

command -v snyk >/dev/null || { echo "snyk not installed: npm install -g snyk"; exit 1; }

echo "snyk        $(snyk --version)"
echo "python      $("$VENV_PY" --version 2>&1)"
echo "results     $OUT_DIR"
echo

# ── 1. dependencies ──────────────────────────────────────────────────────────
echo "=============================================================="
echo " 1/2  Dependencies (requirements.txt)"
echo "=============================================================="
snyk test \
    --command="$VENV_PY" \
    --json-file-output="$OUT_DIR/deps.json"
DEPS_RC=$?
echo

# ── 2. source ────────────────────────────────────────────────────────────────
echo "=============================================================="
echo " 2/2  Source code (SAST)"
echo "=============================================================="
snyk code test \
    --json-file-output="$OUT_DIR/code.json"
CODE_RC=$?
echo

# ── record the verdict ───────────────────────────────────────────────────────
# /api/health reads this file, never the CLI. It exists because the scan
# artifacts alone cannot be trusted to describe what happened: Snyk does not
# write a JSON file for SAST when it finds nothing, so a missing code.json means
# either "clean" or "never ran" and there is no way to tell them apart after the
# fact. Recording the exit codes turns that inference into an assertion.
cat > "$OUT_DIR/summary.json" <<JSON
{
  "scanned_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "snyk_version": "$(snyk --version 2>/dev/null)",
  "dependencies_exit": $DEPS_RC,
  "code_exit": $CODE_RC
}
JSON

# ── verdict ──────────────────────────────────────────────────────────────────
# 0 = clean, 1 = issues found, 2 = the scan itself failed, 3 = nothing to scan.
# Only 2 and 3 mean the scan did not happen; conflating them with 1 is how a
# broken scan gets mistaken for a clean bill of health.
explain() {
    case "$1" in
        0) echo "clean" ;;
        1) echo "ISSUES FOUND" ;;
        2) echo "SCAN FAILED — did not run" ;;
        3) echo "no supported projects detected" ;;
        *) echo "unexpected exit $1" ;;
    esac
}

echo "=============================================================="
printf ' dependencies   %s\n' "$(explain $DEPS_RC)"
printf ' source code    %s\n' "$(explain $CODE_RC)"
echo "=============================================================="

if [ "$DEPS_RC" -ge 2 ] || [ "$CODE_RC" -ge 2 ]; then
    echo
    echo "At least one scan did not complete. Re-run with -d for the real error;"
    echo "the CLI often reports auth failures as a generic project-type error."
    exit 2
fi

if [ "$DEPS_RC" -eq 1 ] || [ "$CODE_RC" -eq 1 ]; then
    echo
    echo "Review the findings above before fixing anything. Do not run"
    echo "\`snyk fix\` unattended on this repo — requirements.txt pins exact"
    echo "versions of trimesh/numpy that the mesh and slicing paths depend on."
    exit 1
fi

echo
echo "No issues found."
