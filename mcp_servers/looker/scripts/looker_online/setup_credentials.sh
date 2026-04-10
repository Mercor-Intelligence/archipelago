#!/usr/bin/env bash
# Interactive script to setup .env file for Looker online mode
#
# Platform notes:
# - Linux/macOS: Run directly
# - Windows: Requires Git Bash, WSL, or similar bash environment
#   Alternative: Manually create .env file following mcp_servers/looker/.env.example

set -e

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color
BOLD='\033[1m'

# Get the directory where this script is located
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
# Go up two levels: scripts/looker_online -> scripts -> repo root
PROJECT_ROOT="$(dirname "$(dirname "$SCRIPT_DIR")")"
ENV_FILE="$PROJECT_ROOT/mcp_servers/looker/.env"

echo -e "${BLUE}${BOLD}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "   🔧 Looker Online Mode Setup"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo -e "${NC}"
echo ""
echo "This script will help you configure your .env file for connecting"
echo "to a Looker instance via the API."
echo ""
echo -e "${YELLOW}📋 You'll need these from your Looker instance:${NC}"
echo "   1. Looker instance URL (e.g., https://your-company.looker.com:19999)"
echo "   2. API Client ID (from Looker Admin > Users > API Keys)"
echo "   3. API Client Secret (from Looker Admin > Users > API Keys)"
echo ""
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

# Check if .env already exists
if [ -f "$ENV_FILE" ]; then
    echo -e "${YELLOW}⚠️  .env file already exists at:${NC}"
    echo "   $ENV_FILE"
    echo ""
    read -p "Do you want to overwrite it? (y/N): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo -e "${RED}❌ Cancelled. Existing .env file preserved.${NC}"
        exit 1
    fi
    echo ""
fi

# Prompt for Looker URL
echo -e "${BOLD}Enter Looker instance URL:${NC}"
echo -e "${BLUE}Example: https://qwiklabs-gcp-03-abc123.looker.com:19999${NC}"
read -p "> " LOOKER_URL

# Validate URL format
if [[ ! $LOOKER_URL =~ ^https?:// ]]; then
    echo -e "${RED}❌ Error: URL must start with http:// or https://${NC}"
    exit 1
fi

# Strip trailing slash if present
LOOKER_URL="${LOOKER_URL%/}"

echo ""

# Prompt for Client ID
echo -e "${BOLD}Enter API Client ID:${NC}"
echo -e "${BLUE}Example: GF9kKt3mPvXbYw2JqR7h${NC}"
read -p "> " CLIENT_ID

if [ -z "$CLIENT_ID" ]; then
    echo -e "${RED}❌ Error: Client ID cannot be empty${NC}"
    exit 1
fi

echo ""

# Prompt for Client Secret (hidden input)
echo -e "${BOLD}Enter API Client Secret:${NC}"
echo -e "${BLUE}Example: 7HnMk3Qp9vFd2XcBwZ8t${NC}"
read -s -p "> " CLIENT_SECRET
echo ""

if [ -z "$CLIENT_SECRET" ]; then
    echo -e "${RED}❌ Error: Client Secret cannot be empty${NC}"
    exit 1
fi

echo ""

# Optional: Ask about SSL verification
echo -e "${BOLD}Verify SSL certificates? (recommended: yes)${NC}"
read -p "Verify SSL? (Y/n): " -n 1 -r
echo
if [[ $REPLY =~ ^[Nn]$ ]]; then
    VERIFY_SSL="false"
else
    VERIFY_SSL="true"
fi

echo ""
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

# Create .env file
cat > "$ENV_FILE" << EOF
# Looker Online Mode Configuration
# Generated: $(date)

# Mode Control
# Note: OFFLINE_MODE is not set, so mode auto-detects based on credentials.
# When credentials are provided, online mode is automatically enabled.
# To force offline mode even with credentials, set: OFFLINE_MODE=true

# Looker API Configuration
LOOKER_BASE_URL=$LOOKER_URL
LOOKER_CLIENT_ID=$CLIENT_ID
LOOKER_CLIENT_SECRET=$CLIENT_SECRET

# Optional settings
LOOKER_VERIFY_SSL=$VERIFY_SSL
LOOKER_TIMEOUT=120
DEBUG=true
EOF

echo -e "${GREEN}✅ Created .env file at:${NC}"
echo "   $ENV_FILE"
echo ""

# Test the connection
echo -e "${BLUE}🔍 Testing OAuth connection...${NC}"
echo ""

cd "$PROJECT_ROOT"

# Run quick OAuth test
if command -v uv &> /dev/null; then
    TEST_RESULT=$(cd mcp_servers/looker && uv run python -c "
import asyncio
import sys
sys.path.insert(0, '.')

from auth import LookerAuthService
from config import settings

async def test():
    try:
        # Use config.settings which loads .env file via pydantic-settings
        auth = LookerAuthService(
            base_url=settings.looker_base_url,
            client_id=settings.looker_client_id,
            client_secret=settings.looker_client_secret,
            verify_ssl=settings.looker_verify_ssl,
        )
        token = await auth.get_access_token()
        print(f'SUCCESS:{token[:20]}')
        return True
    except Exception as e:
        print(f'ERROR:{str(e)}', file=sys.stderr)
        return False

result = asyncio.run(test())
sys.exit(0 if result else 1)
" 2>&1)

    if echo "$TEST_RESULT" | grep -q "^SUCCESS:"; then
        TOKEN_PREFIX=$(echo "$TEST_RESULT" | sed 's/SUCCESS://')
        echo -e "${GREEN}✅ OAuth connection successful!${NC}"
        echo -e "${GREEN}   Token: ${TOKEN_PREFIX}...${NC}"
        echo ""
        echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        echo ""
        echo -e "${GREEN}${BOLD}🚀 Setup complete! You're ready to test online mode.${NC}"
        echo ""
        echo -e "${BOLD}Next steps:${NC}"
        echo ""
        echo "  1. Run automated validation:"
        echo -e "     ${BLUE}uv run python scripts/looker_online/validate_connection.py${NC}"
        echo ""
        echo "  2. Start the MCP server:"
        echo -e "     ${BLUE}cd mcp_servers/looker && uv run python main.py${NC}"
        echo ""
        echo "  3. Capture real data (optional):"
        echo -e "     ${BLUE}uv run python scripts/looker_online/capture_data.py --quick${NC}"
        echo ""
    else
        ERROR_MSG=$(echo "$TEST_RESULT" | grep "^ERROR:" | sed 's/ERROR://')
        echo -e "${RED}❌ OAuth connection failed${NC}"
        echo -e "${RED}   Error: $ERROR_MSG${NC}"
        echo ""
        echo -e "${YELLOW}Common issues:${NC}"
        echo "  • Wrong Client ID or Secret (check Admin > Users > API Keys)"
        echo "  • Looker URL incorrect (should include port, typically :19999)"
        echo "  • API Keys not generated yet (need to create them first)"
        echo "  • Network connectivity issues or firewall blocking access"
        echo ""
        echo -e "${BLUE}Your .env file was created but connection failed.${NC}"
        echo "Fix the credentials and try running this script again."
        echo ""
        exit 1
    fi
else
    echo -e "${YELLOW}⚠️  uv not found, skipping connection test${NC}"
    echo -e "${GREEN}✅ .env file created successfully${NC}"
    echo ""
    echo "Install uv to enable connection testing:"
    echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
    echo ""
fi
