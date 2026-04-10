#!/usr/bin/env bash
# Build Docker image for: data-science-world-506--(world_375b323e57b34515b28c9b890bfe9c9a)
#
# Usage:
#   ./build.sh
#
# Prerequisites:
#   - Run download_data.sh first to fetch data files from S3
#   - Docker must be installed

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

echo "Merging shared code..."
cp -r "$REPO_ROOT/tools/"* "$SCRIPT_DIR/tools/" 2>/dev/null || true
cp -r "$REPO_ROOT/grader/"* "$SCRIPT_DIR/grader/" 2>/dev/null || true

echo "Building Docker image..."
docker build -t "cohere-data-science-world-506--(world_375b323e57b34515b28c9b890bfe9c9a)" "$SCRIPT_DIR"

echo ""
echo "Done! Run with:"
echo "  docker run -e FMP_API_KEY=... -e TERRAPIN_API_KEY=... cohere-data-science-world-506--(world_375b323e57b34515b28c9b890bfe9c9a) bash /app/tools/start.sh <task-slug>"
