#!/usr/bin/env bash
# Accept/reject gate for FMP offline SQLite seeds.
#
# Thin wrapper over scripts/validate_sqlite_seed.py.
# Canonical sample seed: schemas/samples (contains fmp.db).
# schemas/ alone is templates/docs only — missing .db is a FAIL with diagnostics
# (empty/templates dirs are NOT accepted as valid generated seeds).
set -euo pipefail

_on_err() {
    local rc=$?
    echo "ERROR: test-schema-validate.sh failed at line ${BASH_LINENO[0]:-?}: $BASH_COMMAND (exit $rc)"
    echo "ERROR: test-schema-validate.sh failed at line ${BASH_LINENO[0]:-?}: $BASH_COMMAND (exit $rc)" >&2
    exit "$rc"
}
trap '_on_err' ERR

_die() {
    local rc="${1:-1}"
    shift
    echo "$*"
    echo "$*" >&2
    trap - ERR
    exit "$rc"
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "=== FMP SQLite Schema Validation ==="
echo "Validator: scripts/validate_sqlite_seed.py"
echo ""

if [ "$#" -ge 1 ] && [ -n "${1:-}" ]; then
    TARGET="$1"
elif [ -n "${STATE_LOCATION:-}" ] && [ -d "${STATE_LOCATION}" ]; then
    TARGET="$STATE_LOCATION"
else
    TARGET="$REPO_ROOT/schemas/samples"
fi

if [ -d "$TARGET" ]; then
    TARGET="$(cd "$TARGET" && pwd)"
elif [ -d "$REPO_ROOT/$TARGET" ]; then
    TARGET="$(cd "$REPO_ROOT/$TARGET" && pwd)"
fi

echo "Seed directory: $TARGET"
echo ""

if [ ! -d "$TARGET" ]; then
    _die 2 "ERROR: seed directory not found: $TARGET"
fi

VALIDATOR="$REPO_ROOT/scripts/validate_sqlite_seed.py"
exit_code=0

trap - ERR
set +e
if command -v uv >/dev/null 2>&1 \
    && [ -f "$REPO_ROOT/pyproject.toml" ] \
    && [ -d "$REPO_ROOT/.venv" ]; then
    (cd "$REPO_ROOT" && uv run --frozen --no-sync python "$VALIDATOR" "$TARGET")
    exit_code=$?
    if [ "$exit_code" -ne 0 ]; then
        echo "ERROR: validate_sqlite_seed.py failed via 'uv run --frozen --no-sync' (exit $exit_code)"
    fi
else
    (cd "$REPO_ROOT" && python3 "$VALIDATOR" "$TARGET")
    exit_code=$?
fi
set -e
trap '_on_err' ERR

echo ""
if [ "$exit_code" -eq 0 ]; then
    echo "All schema validations passed."
else
    echo "Schema validation failed (exit code $exit_code)."
fi

trap - ERR
exit "$exit_code"
