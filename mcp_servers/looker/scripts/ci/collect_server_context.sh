#!/bin/bash
# Collect context about an MCP server for stage determination
# Usage: ./collect_server_context.sh <server_name>

set -euo pipefail

if [ $# -ne 1 ]; then
  echo "Usage: $0 <server_name>" >&2
  exit 1
fi

SERVER="$1"
SERVER_PATH="mcp_servers/$SERVER"

if [ ! -d "$SERVER_PATH" ]; then
  echo "Error: Server directory not found: $SERVER_PATH" >&2
  exit 1
fi

# ============================================================================
# Stage 1: Foundation
# ============================================================================
echo "=== Stage 1: Foundation ==="

HAS_MAIN_PY=$([ -f "$SERVER_PATH/main.py" ] && echo "true" || echo "false")
HAS_UI_PY=$([ -f "$SERVER_PATH/ui.py" ] && echo "true" || echo "false")
HAS_BUILD_PLAN=$([ -f "$SERVER_PATH/BUILD_PLAN.md" ] && echo "true" || echo "false")
HAS_PRODUCT_SPEC=$([ -f "$SERVER_PATH/PRODUCT_SPEC.md" ] && echo "true" || echo "false")

cat <<EOF
main.py: $HAS_MAIN_PY
ui.py: $HAS_UI_PY
BUILD_PLAN.md: $HAS_BUILD_PLAN
PRODUCT_SPEC.md: $HAS_PRODUCT_SPEC

EOF

# ============================================================================
# Stage 2: Tool Implementation
# ============================================================================
echo "=== Stage 2: Tool Implementation ==="

# Count tests
if [ -d "$SERVER_PATH/tests" ]; then
  TEST_COUNT=$(find "$SERVER_PATH/tests" -name "test_*.py" -type f 2>/dev/null | wc -l | tr -d ' \n' || echo "0")
  TEST_COUNT=${TEST_COUNT:-0}
else
  TEST_COUNT=0
fi

cat <<EOF
Test files: $TEST_COUNT

EOF

# ============================================================================
# Provide File Contents for LLM Analysis
# ============================================================================
echo "=== File Contents for Tool Analysis ==="

# Provide BUILD_PLAN.md content
if [ "$HAS_BUILD_PLAN" = "true" ]; then
  echo ""
  echo "--- BUILD_PLAN.md ---"
  cat "$SERVER_PATH/BUILD_PLAN.md"
  echo ""
  echo "--- END BUILD_PLAN.md ---"
  echo ""
fi

# Provide main.py content
if [ "$HAS_MAIN_PY" = "true" ]; then
  echo ""
  echo "--- main.py ---"
  cat "$SERVER_PATH/main.py"
  echo ""
  echo "--- END main.py ---"
  echo ""
fi

# Provide tools/__init__.py content
if [ -f "$SERVER_PATH/tools/__init__.py" ]; then
  echo ""
  echo "--- tools/__init__.py ---"
  cat "$SERVER_PATH/tools/__init__.py"
  echo ""
  echo "--- END tools/__init__.py ---"
  echo ""
fi

# Provide all tools/*.py files (for Pattern B - grouped registration)
if [ -d "$SERVER_PATH/tools" ]; then
  for tool_file in "$SERVER_PATH/tools"/*.py; do
    if [ -f "$tool_file" ]; then
      filename=$(basename "$tool_file")
      # Skip __init__.py (already provided), _meta_tools.py (not regular tools), and helper files
      if [ "$filename" != "__init__.py" ] && [ "$filename" != "__pycache__" ] && [ "$filename" != "_meta_tools.py" ] && [ "$filename" != "constants.py" ]; then
        echo ""
        echo "--- tools/$filename ---"
        cat "$tool_file"
        echo ""
        echo "--- END tools/$filename ---"
        echo ""
      fi
    fi
  done
fi

echo ""

# ============================================================================
# Stage 3: UI Work
# ============================================================================
echo "=== Stage 3: UI Work ==="

HAS_UI_DIR=$([ -d "ui/$SERVER" ] && echo "true" || echo "false")
HAS_UI_PACKAGE=$([ -f "ui/$SERVER/package.json" ] && echo "true" || echo "false")
HAS_UI_COMPONENTS=$([ -d "ui/$SERVER/components" ] && echo "true" || echo "false")
HAS_UI_API_CONFIG=$([ -f "ui/$SERVER/lib/api-config.ts" ] && echo "true" || echo "false")

cat <<EOF
UI directory exists: $HAS_UI_DIR
package.json: $HAS_UI_PACKAGE
components/: $HAS_UI_COMPONENTS
lib/api-config.ts: $HAS_UI_API_CONFIG

EOF

# ============================================================================
# Stage 4: Meta Tool Registry
# ============================================================================
echo "=== Stage 4: Meta Tool Registry ==="

HAS_META_TOOLS=$([ -f "$SERVER_PATH/tools/_meta_tools.py" ] && echo "true" || echo "false")
HAS_GUI_ENABLED=$(grep -q "GUI_ENABLED" "$SERVER_PATH/main.py" 2>/dev/null && echo "true" || echo "false")

# Check for dual registration pattern
HAS_DUAL_REGISTRATION="false"
if [ "$HAS_GUI_ENABLED" = "true" ] && [ "$HAS_META_TOOLS" = "true" ]; then
  # Verify conditional imports exist
  if grep -q "if GUI_ENABLED:" "$SERVER_PATH/main.py" 2>/dev/null; then
    HAS_DUAL_REGISTRATION="true"
  fi
fi

cat <<EOF
Meta tools file (_meta_tools.py): $HAS_META_TOOLS
GUI_ENABLED in main.py: $HAS_GUI_ENABLED
Dual registration pattern: $HAS_DUAL_REGISTRATION

EOF

# ============================================================================
# Stage 5: Wiki Creation
# ============================================================================
echo "=== Stage 5: Wiki Creation ==="

# Check both server-specific wiki and root wiki directory
HAS_SERVER_WIKI=$([ -d "$SERVER_PATH/wiki" ] && echo "true" || echo "false")
HAS_ROOT_WIKI=$([ -d "wiki" ] && echo "true" || echo "false")

if [ "$HAS_SERVER_WIKI" = "true" ] || [ "$HAS_ROOT_WIKI" = "true" ]; then
  HAS_WIKI="true"
  WIKI_FILES=0
  if [ "$HAS_SERVER_WIKI" = "true" ]; then
    SERVER_WIKI_COUNT=$(find "$SERVER_PATH/wiki" -name "*.md" 2>/dev/null | wc -l | tr -d ' \n' || echo "0")
    WIKI_FILES=$((WIKI_FILES + SERVER_WIKI_COUNT))
  fi
  if [ "$HAS_ROOT_WIKI" = "true" ]; then
    ROOT_WIKI_COUNT=$(find "wiki" -name "*.md" 2>/dev/null | wc -l | tr -d ' \n' || echo "0")
    WIKI_FILES=$((WIKI_FILES + ROOT_WIKI_COUNT))
  fi
else
  HAS_WIKI="false"
  WIKI_FILES=0
fi

# Check for guide.json
HAS_GUIDE_JSON=$([ -f "ui/$SERVER/public/guide.json" ] && echo "true" || echo "false")
GUIDE_IN_SYNC="n/a"

if [ "$HAS_GUIDE_JSON" = "true" ]; then
  # Regenerate to temp location and compare
  TEMP_DIR=$(mktemp -d)
  if uv run python scripts/generate_guide_json.py --server "$SERVER" --output "$TEMP_DIR" 2>/dev/null; then
    if diff -q "ui/$SERVER/public/guide.json" "$TEMP_DIR/guide.json" >/dev/null 2>&1; then
      GUIDE_IN_SYNC="true"
    else
      GUIDE_IN_SYNC="false"
    fi
  fi
  rm -rf "$TEMP_DIR"
fi

cat <<EOF
Wiki directory: $HAS_WIKI
Wiki markdown files: $WIKI_FILES
guide.json exists: $HAS_GUIDE_JSON
guide.json in sync: $GUIDE_IN_SYNC

EOF

# ============================================================================
# Stage 6: QC Testing
# ============================================================================
echo "=== Stage 6: QC Testing ==="

# Stage 6 is automatically reached when Stage 5 passes (wiki + guide.json exist)
if [ "$HAS_WIKI" = "true" ] && [ "$HAS_GUIDE_JSON" = "true" ]; then
  READY_FOR_QC="true"
else
  READY_FOR_QC="false"
fi

cat <<EOF
QC testing ready: $READY_FOR_QC
(Auto-reached when Stage 5 requirements are met)

EOF

# ============================================================================
# Static Flags Summary
# ============================================================================
echo "=== Static Flags ==="

FLAGS=""
[ "$HAS_BUILD_PLAN" = "false" ] && FLAGS="${FLAGS}missing_BUILD_PLAN,"
[ "$HAS_PRODUCT_SPEC" = "false" ] && FLAGS="${FLAGS}missing_PRODUCT_SPEC,"
[ "$GUIDE_IN_SYNC" = "false" ] && FLAGS="${FLAGS}guide_out_of_sync,"

if [ -n "$FLAGS" ]; then
  echo "Flags: ${FLAGS%,}"
else
  echo "Flags: none"
fi
