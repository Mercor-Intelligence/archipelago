#!/bin/bash
#
# Run a task from the mercor/apex-agents HuggingFace dataset.
#
# Usage:
#   cd archipelago/examples/hugging_face_task
#   ./run.sh                    # Run default task (Investment Banking)
#   ./run.sh 42                 # Run task at index 42
#   ./run.sh task_abc123        # Run task by ID
#
# Prerequisites:
#   - Docker running
#   - LLM API key set in agents/.env
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARCHIPELAGO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

export EXAMPLE_DIR="$SCRIPT_DIR"
export ENVIRONMENT_DIR="$ARCHIPELAGO_DIR/environment"
export AGENTS_DIR="$ARCHIPELAGO_DIR/agents"
export GRADING_DIR="$ARCHIPELAGO_DIR/grading"
export ENV_URL="http://localhost:8080"

echo "============================================================"
echo "HUGGING FACE TASK"
echo "============================================================"
echo "Example dir:     $EXAMPLE_DIR"
echo "Archipelago dir: $ARCHIPELAGO_DIR"
echo "============================================================"

# Install agent dependencies
echo "Installing agent dependencies..."
cd "$AGENTS_DIR"
uv sync

# Install huggingface_hub for downloading from HuggingFace dataset
uv pip install -q huggingface_hub

# Install grading dependencies
echo "Installing grading dependencies..."
cd "$GRADING_DIR"
uv sync

# Run main.py with any arguments passed to this script
cd "$AGENTS_DIR" && uv run python "$EXAMPLE_DIR/main.py" "$@"
