#!/usr/bin/env bash
# Accept/reject gate for the EDGAR SEC offline-data seed.
#
# The studio seed-generation pipeline runs this script as its accept gate
# against the generated bundle (`bash scripts/test-schema-validate.sh <seed_dir>`);
# the mise [tasks.test-schema-validate] task delegates here too. It wraps
# scripts/validate_seed.py, the structural validator documented in
# schemas/README.md. No SEED_SCHEMA_MANIFEST marker is emitted, so the
# pipeline treats this script's exit code as the verdict (0 = clean, non-zero
# = rejected).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Directory to validate: explicit arg (how studio and the mise task invoke us)
# > $STATE_LOCATION when it is a real directory > the bundled docs/sample-seed
# so a bare local run validates a real tree instead of erroring on an
# unset/absent STATE_LOCATION.
if [ "$#" -ge 1 ] && [ -n "${1:-}" ]; then
    TARGET="$1"
elif [ -n "${STATE_LOCATION:-}" ] && [ -d "${STATE_LOCATION}" ]; then
    TARGET="$STATE_LOCATION"
else
    TARGET="$REPO_ROOT/docs/sample-seed"
fi

exec python3 "$SCRIPT_DIR/validate_seed.py" "$TARGET"
