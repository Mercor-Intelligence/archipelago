#!/bin/bash
# Start Bloomberg Emulator Server
# This script activates the virtual environment and starts the server

set -e

echo "🚀 Starting Bloomberg MCP Server..."
echo ""

# Check if virtual environment exists
if [ ! -d ".venv" ]; then
    echo "❌ Virtual environment not found!"
    echo "Please create one: python3 -m venv .venv"
    echo "Then install dependencies: pip install -r requirements.txt"
    exit 1
fi

# Activate virtual environment
source .venv/bin/activate

# Check if dependencies are installed
if ! python -c "import fastmcp" 2>/dev/null; then
    echo "❌ Dependencies not installed!"
    echo "Please run: pip install -r requirements.txt"
    exit 1
fi

# Set environment variables
export PYTHONPATH=src
export MOCK_OPENBB=true

echo "✓ Starting MCP server (STDIO mode)"
echo "✓ Press Ctrl+C to stop"
echo ""

# Start server
python -m blpapi_mcp.main
