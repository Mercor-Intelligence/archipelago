#!/usr/bin/env bash
# Accept/reject gate for FMP offline SQLite seeds.
#
# The studio seed-generation pipeline (single_agent_export) runs this script
# as its accept gate against the generated bundle, following the fleet
# convention `bash scripts/test-schema-validate.sh <seed_dir>` (see
# pipelines/functions/single_agent_export in Mercor-io/studio). It is a thin
# wrapper over scripts/validate_sqlite_seed.py, the authoritative structural
# validator documented in schemas/README.md.
#
# This script does NOT emit a SEED_SCHEMA_MANIFEST marker, so the pipeline
# treats its exit code as the verdict directly: 0 = bundle clean, non-zero =
# rejected.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Directory to validate: an explicit argument (how both studio and the mise
# task invoke this) wins; otherwise $STATE_LOCATION when it points at a real
# directory; otherwise the checked-in schemas/samples so a bare
# `bash scripts/test-schema-validate.sh` on a dev machine still validates
# something real. Note: mise.toml does not set STATE_LOCATION in [env], so
# this fallback is reachable locally (class 7).
if [ "$#" -ge 1 ] && [ -n "${1:-}" ]; then
    TARGET="$1"
elif [ -n "${STATE_LOCATION:-}" ] && [ -d "${STATE_LOCATION}" ]; then
    TARGET="$STATE_LOCATION"
else
    TARGET="$REPO_ROOT/schemas/samples"
fi

# Resolve to an absolute path so both the `uv` and bare `python3` invocations
# agree regardless of caller cwd. Prefer the caller's cwd; fall back to paths
# relative to the repo root (studio/mise often pass `schemas/samples`).
if [ -d "$TARGET" ]; then
    TARGET="$(cd "$TARGET" && pwd)"
elif [ -d "$REPO_ROOT/$TARGET" ]; then
    TARGET="$(cd "$REPO_ROOT/$TARGET" && pwd)"
fi

run_validator() {
    if command -v uv >/dev/null 2>&1 && [ -f "$REPO_ROOT/pyproject.toml" ]; then
        (cd "$REPO_ROOT" && uv run --frozen --no-sync python "$REPO_ROOT/scripts/validate_sqlite_seed.py" "$TARGET")
    else
        (cd "$REPO_ROOT" && python3 "$REPO_ROOT/scripts/validate_sqlite_seed.py" "$TARGET")
    fi
}

run_validator
