#!/bin/bash
# Automated Next.js to MCPView Migration Script
# Usage: ./migrate.sh /path/to/mercor-service

set -e

REPO_PATH="${1:-.}"
cd "$REPO_PATH"

# Derive service name from directory
SERVICE_NAME=$(basename "$REPO_PATH" | sed 's/mercor-//')
SERVICE_NAME_PASCAL=$(echo "$SERVICE_NAME" | sed -r 's/(^|-)([a-z])/\U\2/g' 2>/dev/null || echo "$SERVICE_NAME" | awk -F'-' '{for(i=1;i<=NF;i++) $i=toupper(substr($i,1,1)) substr($i,2)}1' OFS='')

echo "=================================================="
echo "Migrating $SERVICE_NAME to MCPView format"
echo "Service: $SERVICE_NAME"
echo "Pascal Case: $SERVICE_NAME_PASCAL"
echo "=================================================="

# Step 1: Validate repository structure
echo ""
echo "[1/8] Validating repository structure..."
if [ ! -d "ui" ]; then
  echo "❌ Error: ui/ folder not found"
  exit 1
fi
if [ ! -d "rls-ui-dev" ]; then
  echo "❌ Error: rls-ui-dev/ folder not found"
  exit 1
fi
echo "✓ Repository structure validated"

# Step 2: Analyze existing UI
echo ""
echo "[2/8] Analyzing existing UI..."

UI_DIR=$(find ui -maxdepth 1 -type d ! -name ui | head -1)
if [ -z "$UI_DIR" ]; then
  echo "❌ Error: No subdirectory found in ui/"
  exit 1
fi
echo "  Found UI directory: $UI_DIR"

# Check for api-config
API_CONFIG="$UI_DIR/lib/api-config.ts"
if [ ! -f "$API_CONFIG" ]; then
  API_CONFIG="$UI_DIR/lib/api-config.js"
fi

if [ -f "$API_CONFIG" ]; then
  echo "  ✓ Found api-config: $API_CONFIG"

  # Extract categories
  CATEGORIES=$(grep "category:" "$API_CONFIG" | sed "s/.*category: ['\"]//;s/['\"].*//" | sort -u | grep -v "^string$" || true)
  CATEGORY_COUNT=$(echo "$CATEGORIES" | wc -l | tr -d ' ')
  echo "  ✓ Found $CATEGORY_COUNT categories"
else
  echo "  ⚠ No api-config found - will use generic categories"
  CATEGORIES=""
fi

# Step 3: Create rls-ui folder
echo ""
echo "[3/8] Creating rls-ui folder..."
mkdir -p rls-ui
echo "✓ Created rls-ui/"

# Step 4: Copy reference MCPView and customize
echo ""
echo "[4/8] Generating ${SERVICE_NAME_PASCAL}MCPView.tsx..."
REFERENCE_VIEW="/Users/neelvenugopal/Development/mercor-quickbooks/rls-ui/QuickbooksMCPView.tsx"

if [ ! -f "$REFERENCE_VIEW" ]; then
  echo "❌ Error: Reference MCPView not found at $REFERENCE_VIEW"
  exit 1
fi

# Copy and customize the view
cp "$REFERENCE_VIEW" "rls-ui/${SERVICE_NAME_PASCAL}MCPView.tsx"

# Replace component name
sed -i.bak "s/QuickbooksMCPView/${SERVICE_NAME_PASCAL}MCPView/g" "rls-ui/${SERVICE_NAME_PASCAL}MCPView.tsx"
sed -i.bak "s/Quickbooks/${SERVICE_NAME_PASCAL}/g" "rls-ui/${SERVICE_NAME_PASCAL}MCPView.tsx"
rm -f "rls-ui/${SERVICE_NAME_PASCAL}MCPView.tsx.bak"

echo "✓ Generated ${SERVICE_NAME_PASCAL}MCPView.tsx"
echo "  ⚠ You'll need to manually update:"
echo "    - Category detection logic (line ~408)"
echo "    - Category colors (line ~159)"
echo "    - Sample data tables (line ~211)"
echo "    - Models/Docs tab content"

# Step 5: Create index.ts
echo ""
echo "[5/8] Creating index.ts..."

cat > "rls-ui/index.ts" << EOF
import type { MCPViewDefinition } from "../../types";

export const ${SERVICE_NAME^^}_MCP_VIEW_DEFINITION: MCPViewDefinition = {
  mcp_view_id: "${SERVICE_NAME}",
  mcp_view_name: "${SERVICE_NAME_PASCAL}",
  mcp_view_description: "MCP interface for ${SERVICE_NAME_PASCAL}",
};

import ${SERVICE_NAME_PASCAL}MCPView from "./${SERVICE_NAME_PASCAL}MCPView";
export { ${SERVICE_NAME_PASCAL}MCPView };
EOF

echo "✓ Created index.ts"

# Step 6: Fix rls-ui-dev infrastructure
echo ""
echo "[6/8] Fixing rls-ui-dev infrastructure..."

# Fix App.tsx import
if [ -f "rls-ui-dev/src/App.tsx" ]; then
  sed -i.bak "s/import { .* as MCPView } from '.\/views';/import { ${SERVICE_NAME_PASCAL}MCPView as MCPView } from '.\/views';/" "rls-ui-dev/src/App.tsx"
  rm -f "rls-ui-dev/src/App.tsx.bak"
  echo "  ✓ Updated App.tsx import"
fi

# Create utils.ts if missing
if [ ! -f "rls-ui-dev/src/utils.ts" ]; then
  cat > "rls-ui-dev/src/utils.ts" << 'EOF'
// Re-export utility functions used by MCP views
export { isErrorResponse } from '@/types';
EOF
  echo "  ✓ Created utils.ts"
fi

# Update main.tsx to remove StrictMode
if [ -f "rls-ui-dev/src/main.tsx" ] && grep -q "StrictMode" "rls-ui-dev/src/main.tsx"; then
  cat > "rls-ui-dev/src/main.tsx" << 'EOF'
import { createRoot } from 'react-dom/client'
import App from './App.tsx'

createRoot(document.getElementById('root')!).render(
  <App />
)
EOF
  echo "  ✓ Removed StrictMode from main.tsx"
fi

# Add isErrorResponse to types if missing
if [ -f "rls-ui-dev/src/types/index.ts" ] && ! grep -q "isErrorResponse" "rls-ui-dev/src/types/index.ts"; then
  cat >> "rls-ui-dev/src/types/index.ts" << 'EOF'

export function isErrorResponse(result: ToolResult): boolean {
  if (!result || !result.content) {
    return false;
  }
  return result.isError === true ||
         result.content.some(c => c.text?.toLowerCase().includes('error'));
}
EOF
  echo "  ✓ Added isErrorResponse to types/index.ts"
fi

echo "  ⚠ Manual fix required: Update src/lib/mcp-client.ts with SSE fix (see implementation-guide.md)"

# Step 7: Sync to rls-ui-dev
echo ""
echo "[7/8] Syncing rls-ui to rls-ui-dev..."
cd rls-ui-dev
bash sync-ui.sh
cd ..

# Step 8: Summary
echo ""
echo "[8/8] Migration Summary"
echo "=================================================="
echo "✓ Created rls-ui/${SERVICE_NAME_PASCAL}MCPView.tsx"
echo "✓ Created rls-ui/index.ts"
echo "✓ Fixed rls-ui-dev infrastructure"
echo "✓ Synced to rls-ui-dev/src/views/"
echo ""
echo "⚠ Manual Steps Required:"
echo "  1. Update category logic in rls-ui/${SERVICE_NAME_PASCAL}MCPView.tsx (line ~408)"
echo "  2. Update category colors (line ~159)"
echo "  3. Update sample data tables if applicable (line ~211)"
echo "  4. Fix SSE handling in rls-ui-dev/src/lib/mcp-client.ts"
echo "  5. Add Models/Docs tab content"
echo ""
echo "Next Steps:"
echo "  1. cd rls-ui-dev && npm run dev"
echo "  2. Start backend: GUI_ENABLED=true MCP_TRANSPORT=http MCP_PORT=5001 mise run start"
echo "  3. Test at http://localhost:5173/"
echo "  4. Commit changes when ready"
echo "=================================================="
