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

# Check for BUILD_PLAN in server path first, then root level
if [ -f "$SERVER_PATH/BUILD_PLAN.md" ]; then
  HAS_BUILD_PLAN="true"
  BUILD_PLAN_PATH="$SERVER_PATH/BUILD_PLAN.md"
elif [ -f "BUILD_PLAN.md" ]; then
  HAS_BUILD_PLAN="true"
  BUILD_PLAN_PATH="BUILD_PLAN.md"
else
  HAS_BUILD_PLAN="false"
  BUILD_PLAN_PATH=""
fi

# Check for BUILD_PLAN_PHASE2 (additional tool definitions)
if [ -f "$SERVER_PATH/BUILD_PLAN_PHASE2.md" ]; then
  HAS_BUILD_PLAN_PHASE2="true"
  BUILD_PLAN_PHASE2_PATH="$SERVER_PATH/BUILD_PLAN_PHASE2.md"
elif [ -f "BUILD_PLAN_PHASE2.md" ]; then
  HAS_BUILD_PLAN_PHASE2="true"
  BUILD_PLAN_PHASE2_PATH="BUILD_PLAN_PHASE2.md"
else
  HAS_BUILD_PLAN_PHASE2="false"
  BUILD_PLAN_PHASE2_PATH=""
fi

# Check for PRODUCT_SPEC in server path first, then root level
if [ -f "$SERVER_PATH/PRODUCT_SPEC.md" ]; then
  HAS_PRODUCT_SPEC="true"
elif [ -f "PRODUCT_SPEC.md" ]; then
  HAS_PRODUCT_SPEC="true"
else
  HAS_PRODUCT_SPEC="false"
fi

cat <<EOF
main.py: $HAS_MAIN_PY
ui.py: $HAS_UI_PY
BUILD_PLAN.md: $HAS_BUILD_PLAN
BUILD_PLAN_PHASE2.md: $HAS_BUILD_PLAN_PHASE2
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
if [ "$HAS_BUILD_PLAN" = "true" ] && [ -n "$BUILD_PLAN_PATH" ]; then
  echo ""
  echo "--- BUILD_PLAN.md ---"
  cat "$BUILD_PLAN_PATH"
  echo ""
  echo "--- END BUILD_PLAN.md ---"
  echo ""
fi

# Provide BUILD_PLAN_PHASE2.md content (if exists)
if [ "$HAS_BUILD_PLAN_PHASE2" = "true" ] && [ -n "$BUILD_PLAN_PHASE2_PATH" ]; then
  echo ""
  echo "--- BUILD_PLAN_PHASE2.md ---"
  cat "$BUILD_PLAN_PHASE2_PATH"
  echo ""
  echo "--- END BUILD_PLAN_PHASE2.md ---"
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
  if [ -n "$TEMP_DIR" ] && [ -d "$TEMP_DIR" ]; then
    # Use server-specific wiki if it exists, otherwise use root wiki
    if [ "$HAS_SERVER_WIKI" = "true" ]; then
      WIKI_DIR_ARG="--wiki-dir $SERVER_PATH/wiki"
    else
      WIKI_DIR_ARG=""
    fi
    if uv run python scripts/generate_guide_json.py --server "$SERVER" $WIKI_DIR_ARG --output "$TEMP_DIR" 2>/dev/null; then
      if diff -q "ui/$SERVER/public/guide.json" "$TEMP_DIR/guide.json" >/dev/null 2>&1; then
        GUIDE_IN_SYNC="true"
      else
        GUIDE_IN_SYNC="false"
      fi
    fi
    rm -rf "$TEMP_DIR"
  fi
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
# Offline Mode Detection
# ============================================================================
echo "=== Offline Mode ==="

# Check for offline provider directory
HAS_OFFLINE_DIR=$([ -d "$SERVER_PATH/providers/offline" ] && echo "true" || echo "false")

# Check for online provider directory (for dual-mode detection)
HAS_ONLINE_DIR=$([ -d "$SERVER_PATH/providers/online" ] && echo "true" || echo "false")

# Determine if dual-mode (has both online and offline providers)
if [ "$HAS_OFFLINE_DIR" = "true" ] && [ "$HAS_ONLINE_DIR" = "true" ]; then
  IS_DUAL_MODE="true"
else
  IS_DUAL_MODE="false"
fi

# Count offline provider files (excluding __init__.py, __pycache__, _base.py)
if [ "$HAS_OFFLINE_DIR" = "true" ]; then
  OFFLINE_PROVIDER_COUNT=$(find "$SERVER_PATH/providers/offline" -maxdepth 1 -name "*.py" ! -name "__init__.py" ! -name "_base.py" -type f 2>/dev/null | wc -l | tr -d ' \n' || echo "0")
  OFFLINE_PROVIDER_COUNT=${OFFLINE_PROVIDER_COUNT:-0}

  # List offline provider files for reference
  OFFLINE_PROVIDERS=$(find "$SERVER_PATH/providers/offline" -maxdepth 1 -name "*.py" ! -name "__init__.py" ! -name "_base.py" -type f 2>/dev/null | xargs -I {} basename {} .py | sort | tr '\n' ',' | sed 's/,$//')
else
  OFFLINE_PROVIDER_COUNT=0
  OFFLINE_PROVIDERS=""
fi

# Detect storage type
STORAGE_TYPE="none"

# Check for SQLite (use grep -r with --include for proper recursive search)
if [ -d "$SERVER_PATH/db" ] || grep -rq --include="*.py" "sqlite" "$SERVER_PATH" 2>/dev/null; then
  STORAGE_TYPE="sqlite"
fi

# Check for DuckDB (overrides sqlite if found)
if grep -rq --include="*.py" "duckdb" "$SERVER_PATH" 2>/dev/null; then
  STORAGE_TYPE="duckdb"
fi

# Check for JSON fixtures
if [ -d "$SERVER_PATH/fixtures" ] || [ -d "$SERVER_PATH/data" ]; then
  JSON_COUNT=$(find "$SERVER_PATH" -path "*fixtures*" -name "*.json" -o -path "*data*" -name "*.json" 2>/dev/null | wc -l | tr -d ' \n' || echo "0")
  if [ "$JSON_COUNT" -gt 0 ] && [ "$STORAGE_TYPE" = "none" ]; then
    STORAGE_TYPE="json"
  fi
fi

# Check for CSV snapshots
CSV_COUNT=$(find "$SERVER_PATH" -path "*snapshots*" -name "*.csv" -o -path "*data*" -name "*.csv" 2>/dev/null | wc -l | tr -d ' \n' || echo "0")
if [ "$CSV_COUNT" -gt 0 ] && [ "$STORAGE_TYPE" = "none" ]; then
  STORAGE_TYPE="csv"
fi

cat <<EOF
Offline provider directory: $HAS_OFFLINE_DIR
Online provider directory: $HAS_ONLINE_DIR
Dual mode (online + offline): $IS_DUAL_MODE
Storage type: $STORAGE_TYPE
Offline provider count: $OFFLINE_PROVIDER_COUNT
Offline providers: $OFFLINE_PROVIDERS

EOF

# ============================================================================
# Static Flags Summary
# ============================================================================
echo "=== Static Flags ==="

FLAGS=""
# Only flag missing BUILD_PLAN if neither BUILD_PLAN nor BUILD_PLAN_PHASE2 exists
if [ "$HAS_BUILD_PLAN" = "false" ] && [ "$HAS_BUILD_PLAN_PHASE2" = "false" ]; then
  FLAGS="${FLAGS}missing_BUILD_PLAN,"
fi
[ "$HAS_PRODUCT_SPEC" = "false" ] && FLAGS="${FLAGS}missing_PRODUCT_SPEC,"
[ "$GUIDE_IN_SYNC" = "false" ] && FLAGS="${FLAGS}guide_out_of_sync,"

if [ -n "$FLAGS" ]; then
  echo "Flags: ${FLAGS%,}"
else
  echo "Flags: none"
fi
