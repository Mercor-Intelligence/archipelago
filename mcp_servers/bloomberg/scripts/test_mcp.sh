#!/bin/bash
# Test Bloomberg Emulator MCP Integration
# Usage: ./test_mcp.sh [method]
# Methods: inspector, manual, client

set -e

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}Bloomberg MCP Test${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""

# Navigate to project root
cd "$(dirname "$0")/.."
PROJECT_ROOT="$(pwd)"

# Activate virtual environment
if [ -f "$PROJECT_ROOT/.venv/bin/activate" ]; then
    source "$PROJECT_ROOT/.venv/bin/activate"
    echo -e "${GREEN}✓${NC} Virtual environment activated"
else
    echo -e "${YELLOW}⚠${NC}  No virtual environment found at $PROJECT_ROOT/.venv"
    echo "Please create one: python3 -m venv .venv"
    echo "Then install: pip install -r requirements.txt"
    exit 1
fi

# Set environment
export PYTHONPATH="$PROJECT_ROOT/src"
export MOCK_OPENBB=true

METHOD="${1:-inspector}"

case $METHOD in
    inspector)
        echo -e "${BLUE}Starting MCP Inspector...${NC}"
        echo ""
        echo "This will:"
        echo "  1. Start the Bloomberg MCP server"
        echo "  2. Open web interface at http://localhost:5173"
        echo "  3. Allow interactive testing of MCP tools"
        echo ""
        echo -e "${YELLOW}Press Ctrl+C to stop${NC}"
        echo ""

        # Check if npx is available
        if ! command -v npx &> /dev/null; then
            echo -e "${YELLOW}⚠${NC}  npx not found. Install Node.js first:"
            echo "  brew install node"
            exit 1
        fi

        # Run MCP Inspector
        npx @modelcontextprotocol/inspector python -m blpapi_mcp.main
        ;;

    manual)
        echo -e "${BLUE}Starting MCP Server (manual mode)...${NC}"
        echo ""
        echo "Server will start on http://0.0.0.0:8001"
        echo ""
        echo -e "${YELLOW}Press Ctrl+C to stop${NC}"
        echo ""

        python -m blpapi_mcp.main
        ;;

    client)
        echo -e "${BLUE}Running Python MCP Client Test...${NC}"
        echo ""

        # Check if mcp package is installed
        if ! python -c "import mcp" 2>/dev/null; then
            echo "Installing MCP Python SDK..."
            pip install -q mcp
        fi

        # Check if test client exists
        if [ ! -f "test_mcp_client.py" ]; then
            echo -e "${YELLOW}⚠${NC}  test_mcp_client.py not found"
            echo "See docs/MCP_TESTING.md for the test script"
            exit 1
        fi

        python test_mcp_client.py
        ;;

    *)
        echo "Usage: $0 [method]"
        echo ""
        echo "Methods:"
        echo "  inspector - Start MCP Inspector (web UI) [default]"
        echo "  manual    - Start server for manual JSON-RPC testing"
        echo "  client    - Run Python MCP client test"
        echo ""
        echo "Examples:"
        echo "  ./test_mcp.sh              # Use MCP Inspector"
        echo "  ./test_mcp.sh inspector    # Same as above"
        echo "  ./test_mcp.sh manual       # Manual testing"
        exit 1
        ;;
esac
