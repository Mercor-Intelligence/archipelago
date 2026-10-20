#!/usr/bin/env bash
# Accept/reject gate for FMP offline SQLite seeds.
# Stdlib-only validator — never route through `uv sync` / `uv run` (install
# noise truncates real errors in fleet grading capture).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

_fail() {
    echo "=== 1 Validation Error(s) ==="
    echo "=== 1 Validation Error(s) ===" >&2
    echo "  1. $*"
    echo "  1. $*" >&2
    exit 1
}

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
    _fail "seed directory not found: $TARGET"
fi

export PYTHONUNBUFFERED=1
if command -v python3 >/dev/null 2>&1; then
    PY=python3
elif command -v python >/dev/null 2>&1; then
    PY=python
else
    _fail "python3/python not found on PATH"
fi

set +e
(cd "$REPO_ROOT" && "$PY" "$REPO_ROOT/scripts/validate_sqlite_seed.py" "$TARGET")
exit_code=$?
set -e

echo ""
if [ "$exit_code" -eq 0 ]; then
    echo "All schema validations passed."
else
    echo "Schema validation failed (exit code $exit_code)."
fi
exit "$exit_code"
