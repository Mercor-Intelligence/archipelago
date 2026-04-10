#!/usr/bin/env bash
# Build Docker image for: apex-v2-combined-tasks-clone--(world_287b9c7307344df2bd8bc1d64cb5b0bf)
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
docker build -t "cohere-apex-v2-combined-tasks-clone--(world_287b9c7307344df2bd8bc1d64cb5b0bf)" "$SCRIPT_DIR"

echo ""
echo "Done! Run with:"
echo "  docker run -e FMP_API_KEY=... -e TERRAPIN_API_KEY=... cohere-apex-v2-combined-tasks-clone--(world_287b9c7307344df2bd8bc1d64cb5b0bf) bash /app/tools/start.sh <task-slug>"
