#!/bin/bash
# FMP MCP Server Wrapper Script
# Used by MCP inspector and other tools that need to spawn the server
#
# This script ensures:
# - Correct working directory (where pyproject.toml is located)
# - Proper Python environment via uv
# - Module-based execution for clean imports

# Get the directory where this script is located
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Change to the script directory (ensures uv finds pyproject.toml)
cd "$SCRIPT_DIR"

# Run the server via uv with module invocation
# Using exec replaces the shell process with Python for clean process management
exec uv run python -m mcp_servers.fmp_server.main
