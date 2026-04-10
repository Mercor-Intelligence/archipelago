#!/bin/bash
# Wrapper script to run the Xero MCP server with proper environment

# Get the directory where this script is located
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Set up environment
export PYTHONPATH="$SCRIPT_DIR/src"
export XERO_OFFLINE_MODE="${XERO_OFFLINE_MODE:-true}"

# Run the server
exec "$SCRIPT_DIR/.venv/bin/python3" -m mcp_servers.xero.main
