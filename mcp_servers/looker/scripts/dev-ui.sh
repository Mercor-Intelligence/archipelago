#!/bin/bash
# Start local UI development environment
# Installs mercor-mcp from git and runs mcp-ui

# Kill any processes running on ports 8000 and 3000
echo "Checking for processes on ports 8000 and 3000..."
lsof -ti :8000 | xargs kill -9 2>/dev/null || true
lsof -ti :3000 | xargs kill -9 2>/dev/null || true

uv pip install "mercor-mcp @ git+https://github.com/Mercor-Intelligence/mercor-mcp.git@main" -q
uv run mcp-ui "$@"
