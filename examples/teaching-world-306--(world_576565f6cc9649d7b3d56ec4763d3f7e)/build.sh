#!/usr/bin/env bash
# Build Docker image for: teaching-world-306--(world_576565f6cc9649d7b3d56ec4763d3f7e)
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
docker build -t "cohere-teaching-world-306--(world_576565f6cc9649d7b3d56ec4763d3f7e)" "$SCRIPT_DIR"

echo ""
echo "Done! Run with:"
echo "  docker run -e FMP_API_KEY=... -e TERRAPIN_API_KEY=... cohere-teaching-world-306--(world_576565f6cc9649d7b3d56ec4763d3f7e) bash /app/tools/start.sh <task-slug>"
