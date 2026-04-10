#!/bin/bash
set -e

# Docker World start.sh
# Usage: docker run <image> [task_slug]
#
# This script:
# 1. Validates required environment variables (secrets)
# 2. Parses task slug from CLI arg or env var
# 3. Sets up task-specific files (3-phase backup/reset/overlay)
# 4. Starts runner + MCP servers in parallel
# 5. Waits for readiness and configures via POST /apps
# 6. Supervises processes and exits if any child dies

# Ensure we run from tools directory so relative paths work
cd /app/tools 2>/dev/null || cd /app

# =============================================================================
# PHASE 0: Validate required secrets
# =============================================================================
validate_secrets() {
    local required_secrets_path="/app/tools/required_secrets.txt"
    local missing=()

    if [ -f "$required_secrets_path" ]; then
        while IFS= read -r var_name; do
            var_name="$(echo "$var_name" | tr -d '\r' | xargs || true)"
            [ -z "$var_name" ] && continue
            if [ -z "${!var_name:-}" ]; then
                missing+=("$var_name")
            fi
        done < "$required_secrets_path"
    else
        # Backward-compatible fallback: scan mcp.json for SECRET/ markers
        local mcp_json_path="/app/tools/mcp.json"
        if [ -f "$mcp_json_path" ]; then
    local secret_vars
    secret_vars=$(grep -oE '"SECRET/[A-Za-z_][A-Za-z0-9_]*"' "$mcp_json_path" 2>/dev/null | sed 's/"SECRET\/\(.*\)"/\1/' | sort -u || true)
    for var_name in $secret_vars; do
                if [ -z "${!var_name:-}" ]; then
            missing+=("$var_name")
        fi
    done
        fi
    fi

    if [ ${#missing[@]} -gt 0 ]; then
        echo ""
        echo "=========================================="
        echo "ERROR: Missing required environment variables"
        echo "=========================================="
        echo ""
        echo "The following secrets must be provided via -e or --env-file:"
        echo ""
        for s in "${missing[@]}"; do
            echo "  - $s"
        done
        echo ""
        echo "Example:"
        echo "  docker run -e ${missing[0]}=your-value ..."
        echo ""
        echo "Or copy .env.template to .env, fill in values, and use:"
        echo "  docker run --env-file .env ..."
        echo ""
        exit 1
    fi
}

validate_secrets

# Parse task slug: CLI arg takes precedence over env var
if [ -n "${1:-}" ]; then
    TASK_SLUG="$1"
else
    TASK_SLUG="${TASK_SLUG:-}"
fi

if [ -n "$TASK_SLUG" ]; then
    # Guard against path traversal / invalid task slugs
    case "$TASK_SLUG" in
        *"/"*|*".."*|*'\\'* )
            echo "ERROR: Invalid TASK_SLUG: $TASK_SLUG"
            exit 1
            ;;
    esac

    echo "Setting up task: $TASK_SLUG"
else
    echo "No task slug provided; starting with world baseline only"
fi

# =============================================================================
# PHASE 1: First-run backup of world baselines (idempotent)
# =============================================================================
if [ ! -d "/app/_world_files_base" ]; then
    mkdir -p "/app/_world_files_base"
    [ -d "/app/files" ] && cp -R "/app/files/." "/app/_world_files_base/" 2>/dev/null || true
fi

if [ ! -d "/app/_world_apps_data_base" ]; then
    mkdir -p "/app/_world_apps_data_base"
    if [ -d "/app/tools/.apps_data" ]; then
        for entry in /app/tools/.apps_data/*; do
            name=$(basename "$entry")
            # Skip if this is a task slug (has matching .json in tasks/)
            [ -f "/app/tasks/${name}.json" ] && continue
            cp -R "$entry" "/app/_world_apps_data_base/" 2>/dev/null || true
        done
    fi
fi

# =============================================================================
# PHASE 2: Reset runtime directories to world baseline
# =============================================================================
rm -rf /app/files/* /.apps_data/*
mkdir -p /app/files /.apps_data
[ -d "/app/_world_files_base" ] && cp -R "/app/_world_files_base/." /app/files/ 2>/dev/null || true
[ -d "/app/_world_apps_data_base" ] && cp -R "/app/_world_apps_data_base/." /.apps_data/ 2>/dev/null || true

# =============================================================================
# PHASE 3: Overlay task-specific files
# =============================================================================
if [ -n "$TASK_SLUG" ]; then
    [ -d "/app/tools/.apps_data/$TASK_SLUG" ] && cp -R "/app/tools/.apps_data/$TASK_SLUG/." /.apps_data/
    [ -d "/app/tools/files/$TASK_SLUG" ] && cp -R "/app/tools/files/$TASK_SLUG/." /app/files/
    echo "Task files setup complete"
else
    echo "Task overlay skipped; world baseline ready"
fi

# =============================================================================
# PHASE 4: Start runner and MCP servers in parallel
# =============================================================================
CHILD_PIDS=""

cleanup_children() {
    for pid in $CHILD_PIDS; do
        kill -TERM "$pid" 2>/dev/null || true
    done
    wait 2>/dev/null || true
}

on_term() {
    echo "Shutting down..."
    cleanup_children
    exit 0
}

on_exit() {
    status=$?
    trap - EXIT
    # If we exit early (e.g., warmup failure under set -e), ensure children are terminated.
    if [ "$status" -ne 0 ]; then
        cleanup_children
    fi
    exit "$status"
}

# Handle shutdown during warmup/startup. The supervision loop installs its own trap later,
# but warmup can take time and we still want graceful termination and cleanup on failure.
trap on_term TERM INT
trap on_exit EXIT

echo "Starting runner on port 8000..."
(
    cd /app/tools
    export PORT=8000
    export HOME="/root"
    umask 0002
    exec uv run --no-sync python -m runner.main
) &
RUNNER_PID=$!
CHILD_PIDS="$RUNNER_PID"

echo "Starting MCP server: code_execution on port 8100..."
(
    cd /app/tools/mcp_servers/code_execution
    export MCP_PORT=8100
    export MCP_TRANSPORT=http
    export APP_FS_ROOT='/filesystem'
    export MCP_TRANSPORT='http'
    export SANDBOX_LIBRARY_PATH='/app/lib/sandbox_fs.so'
    export CODE_EXEC_COMMAND_TIMEOUT='300'
    export GUI_ENABLED='true'
    exec bash -c 'cd mcp_servers/code_execution_server && uv run --no-sync python main.py'
) &
CHILD_PIDS="$CHILD_PIDS $!"

echo "Starting MCP server: excel on port 8101..."
(
    cd /app/tools/mcp_servers/excel
    export MCP_PORT=8101
    export MCP_TRANSPORT=http
    export APP_FS_ROOT='/filesystem'
    export MCP_TRANSPORT='http'
    export APP_SHEETS_ROOT=''
    export LIBREOFFICE_TIMEOUT='30'
    export SKIP_FORMULA_RECALC='false'
    export USE_INDIVIDUAL_TOOLS='true'
    export GUI_ENABLED='true'
    exec bash -c 'cd mcp_servers/sheets_server && uv run --no-sync python main.py'
) &
CHILD_PIDS="$CHILD_PIDS $!"

echo "Starting MCP server: filesystem on port 8102..."
(
    cd /app/tools/mcp_servers/filesystem
    export MCP_PORT=8102
    export MCP_TRANSPORT=http
    export APP_FS_ROOT='/filesystem'
    export MCP_TRANSPORT='http'
    export GUI_ENABLED='true'
    exec bash -c 'cd mcp_servers/filesystem_server && uv run --no-sync python main.py'
) &
CHILD_PIDS="$CHILD_PIDS $!"

echo "Starting MCP server: looker on port 8103..."
(
    cd /app/tools/mcp_servers/looker
    export MCP_PORT=8103
    export MCP_TRANSPORT=http
    export NC=''
    export RED=''
    export BLUE=''
    export BOLD=''
    export FLAGS=''
    export GREEN=''
    export REPLY=''
    export TOOLS=''
    export OSTYPE=''
    export SERVER=''
    export YELLOW=''
    export ENV_FILE=''
    export HAS_WIKI=''
    export LOGLEVEL='INFO'
    export LOG_FILE=''
    export PID_FILE=''
    export TEMP_DIR=''
    export CLIENT_ID=''
    export ERROR_MSG=''
    export HAS_STATE='true'
    export HAS_UI_PY=''
    export JSON_LOGS=''
    export WATCH_CMD=''
    export HAS_UI_DIR=''
    export LOOKER_URL=''
    export OUTPUT_DIR=''
    export SCRIPT_DIR=''
    export TEST_COUNT=''
    export VERIFY_SSL=''
    export WIKI_FILES=''
    export APP_FS_ROOT='/filesystem'
    export BASH_SOURCE=''
    export BUILD_IMAGE=''
    export DAEMON_MODE=''
    export ENABLE_AUTH=''
    export GUI_ENABLED='true'
    export HAS_MAIN_PY=''
    export RLS_API_URL='https://api.studio.mercor.com'
    export SERVER_PATH=''
    export TEST_RESULT=''
    export DATABASE_URL=''
    export DISABLE_AUTH=''
    export OFFLINE_MODE='true'
    export PROJECT_ROOT=''
    export READY_FOR_QC=''
    export TOKEN_PREFIX=''
    export CLIENT_SECRET=''
    export GUIDE_IN_SYNC=''
    export HAS_ROOT_WIKI=''
    export MCP_TRANSPORT='http'
    export APPS_DATA_ROOT=''
    export HAS_BUILD_PLAN=''
    export HAS_GUIDE_JSON=''
    export HAS_META_TOOLS=''
    export HAS_UI_PACKAGE=''
    export LOOKER_API_KEY=''
    export RLS_COMPANY_ID=''
    export STATE_LOCATION='/.apps_data/looker'
    export DATABASE_SCHEMA='public'
    export HAS_GUI_ENABLED=''
    export HAS_SERVER_WIKI=''
    export LOOKER_BASE_URL=''
    export RLS_CAMPAIGN_ID=''
    export HAS_PRODUCT_SPEC=''
    export INTERNET_ENABLED='false'
    export LOOKER_CLIENT_ID=''
    export ENVIRONMENT_IMAGE=''
    export HAS_UI_API_CONFIG=''
    export HAS_UI_COMPONENTS=''
    export ORCHESTRATOR_MODEL=''
    export CLAUDE_PROJECTS_DIR=''
    export LOOKER_PROJECT_NAME=''
    export MCP_LOCAL_FILES_DIR=''
    export __UPPER_NAME___MODE=''
    export LOOKER_CLIENT_SECRET=''
    export OFFLINE_POSTGRES_URL=''
    export HAS_DUAL_REGISTRATION=''
    export LOOKER_WEBHOOK_SECRET=''
    export LOOKER_CONNECTION_NAME='database'
    export LOOKER_PROJECT_GIT_URL=''
    export __UPPER_NAME___API_URL=''
    export LOOKER_PROJECT_GIT_BRANCH='main'
    exec bash -c 'uv run --no-sync python mcp_servers/looker/main.py'
) &
CHILD_PIDS="$CHILD_PIDS $!"

echo "Starting MCP server: pdfs on port 8104..."
(
    cd /app/tools/mcp_servers/pdfs
    export MCP_PORT=8104
    export MCP_TRANSPORT=http
    export APP_FS_ROOT='/filesystem'
    export MCP_TRANSPORT='http'
    export USE_INDIVIDUAL_TOOLS='true'
    export GUI_ENABLED='true'
    exec bash -c 'cd mcp_servers/pdf_server && uv run --no-sync python main.py'
) &
CHILD_PIDS="$CHILD_PIDS $!"

echo "Starting MCP server: powerpoint on port 8105..."
(
    cd /app/tools/mcp_servers/powerpoint
    export MCP_PORT=8105
    export MCP_TRANSPORT=http
    export APP_FS_ROOT='/filesystem'
    export MCP_TRANSPORT='http'
    export APP_SLIDES_ROOT=''
    export USE_INDIVIDUAL_TOOLS='true'
    export GUI_ENABLED='true'
    exec bash -c 'cd mcp_servers/slides_server && uv run --no-sync python main.py'
) &
CHILD_PIDS="$CHILD_PIDS $!"

echo "Starting MCP server: sql_execution on port 8106..."
(
    cd /app/tools/mcp_servers/sql_execution
    export MCP_PORT=8106
    export MCP_TRANSPORT=http
    export HAS_STATE='true'
    export APP_FS_ROOT='/filesystem'
    export MCP_TRANSPORT='http'
    export STATE_LOCATION='/.apps_data/sql_execution'
    export GUI_ENABLED='true'
    exec bash -c 'cd mcp_servers/sql_execution_server && uv run --no-sync python main.py'
) &
CHILD_PIDS="$CHILD_PIDS $!"

echo "Starting MCP server: tableau on port 8107..."
(
    cd /app/tools/mcp_servers/tableau
    export MCP_PORT=8107
    export MCP_TRANSPORT=http
    export LOGLEVEL=''
    export LOG_FILE=''
    export HAS_STATE='true'
    export JSON_LOGS=''
    export PYTHONPATH='.'
    export APP_FS_ROOT='/filesystem'
    export GUI_ENABLED='true'
    export MCP_CSV_DIR=''
    export DATABASE_URL='sqlite+aiosqlite:////.apps_data/tableau/data.db'
    export TABLEAU_MODE=''
    export MCP_TRANSPORT='http'
    export STATE_LOCATION='/.apps_data/tableau'
    export TABLEAU_API_URL=''
    export TABLEAU_SITE_ID='mercor_tableau_mcp'
    export INTERNET_ENABLED='false'
    export TABLEAU_BASE_URL=''
    export TABLEAU_TEST_MODE='local'
    export TABLEAU_AUTH_TOKEN=''
    export TABLEAU_SERVER_URL=''
    export TABLEAU_TOKEN_NAME=''
    export TABLEAU_API_VERSION=''
    export TABLEAU_DATABASE_URL='sqlite+aiosqlite:////.apps_data/tableau/data.db'
    export TABLEAU_TOKEN_SECRET=''
    export RLS_GITHUB_READ_TOKEN=''
    exec bash -c 'uv run --no-sync python mcp_servers/tableau/main.py'
) &
CHILD_PIDS="$CHILD_PIDS $!"

echo "Starting MCP server: word on port 8108..."
(
    cd /app/tools/mcp_servers/word
    export MCP_PORT=8108
    export MCP_TRANSPORT=http
    export APP_FS_ROOT='/filesystem'
    export APP_DOCS_ROOT='/docs'
    export MCP_TRANSPORT='http'
    export USE_INDIVIDUAL_TOOLS='true'
    export GUI_ENABLED='true'
    export INTERNET_ENABLED='false'
    exec bash -c 'cd mcp_servers/docs_server && uv run --no-sync python main.py'
) &
CHILD_PIDS="$CHILD_PIDS $!"

# =============================================================================
# PHASE 5: Run populate hooks (database initialization, etc.)
# =============================================================================
echo "Running populate hook for: code_execution..."
(
    cd /app/tools/mcp_servers/code_execution
    export MCP_PORT='5000'
    export APP_FS_ROOT='/filesystem'
    export MCP_TRANSPORT='http'
    export SANDBOX_LIBRARY_PATH='/app/lib/sandbox_fs.so'
    export CODE_EXEC_COMMAND_TIMEOUT='300'
    export GUI_ENABLED='true'
    bash -c 'echo '"'"'No data to populate'"'"''
)
echo "Populate hook completed: code_execution"

echo "Running populate hook for: excel..."
(
    cd /app/tools/mcp_servers/excel
    export MCP_PORT='5000'
    export APP_FS_ROOT='/filesystem'
    export MCP_TRANSPORT='http'
    export APP_SHEETS_ROOT=''
    export LIBREOFFICE_TIMEOUT='30'
    export SKIP_FORMULA_RECALC='false'
    export USE_INDIVIDUAL_TOOLS='true'
    export GUI_ENABLED='true'
    bash -c 'echo '"'"'No data to populate'"'"''
)
echo "Populate hook completed: excel"

echo "Running populate hook for: filesystem..."
(
    cd /app/tools/mcp_servers/filesystem
    export MCP_PORT='5000'
    export APP_FS_ROOT='/filesystem'
    export MCP_TRANSPORT='http'
    export GUI_ENABLED='true'
    bash -c 'echo '"'"'No data to populate'"'"''
)
echo "Populate hook completed: filesystem"

echo "Running populate hook for: looker..."
(
    cd /app/tools/mcp_servers/looker
    export NC=''
    export RED=''
    export BLUE=''
    export BOLD=''
    export FLAGS=''
    export GREEN=''
    export REPLY=''
    export TOOLS=''
    export OSTYPE=''
    export SERVER=''
    export YELLOW=''
    export ENV_FILE=''
    export HAS_WIKI=''
    export LOGLEVEL='INFO'
    export LOG_FILE=''
    export MCP_PORT='5000'
    export PID_FILE=''
    export TEMP_DIR=''
    export CLIENT_ID=''
    export ERROR_MSG=''
    export HAS_STATE='true'
    export HAS_UI_PY=''
    export JSON_LOGS=''
    export WATCH_CMD=''
    export HAS_UI_DIR=''
    export LOOKER_URL=''
    export OUTPUT_DIR=''
    export SCRIPT_DIR=''
    export TEST_COUNT=''
    export VERIFY_SSL=''
    export WIKI_FILES=''
    export APP_FS_ROOT='/filesystem'
    export BASH_SOURCE=''
    export BUILD_IMAGE=''
    export DAEMON_MODE=''
    export ENABLE_AUTH=''
    export GUI_ENABLED='true'
    export HAS_MAIN_PY=''
    export RLS_API_URL='https://api.studio.mercor.com'
    export SERVER_PATH=''
    export TEST_RESULT=''
    export DATABASE_URL=''
    export DISABLE_AUTH=''
    export OFFLINE_MODE='true'
    export PROJECT_ROOT=''
    export READY_FOR_QC=''
    export TOKEN_PREFIX=''
    export CLIENT_SECRET=''
    export GUIDE_IN_SYNC=''
    export HAS_ROOT_WIKI=''
    export MCP_TRANSPORT='http'
    export APPS_DATA_ROOT=''
    export HAS_BUILD_PLAN=''
    export HAS_GUIDE_JSON=''
    export HAS_META_TOOLS=''
    export HAS_UI_PACKAGE=''
    export LOOKER_API_KEY=''
    export RLS_COMPANY_ID=''
    export STATE_LOCATION='/.apps_data/looker'
    export DATABASE_SCHEMA='public'
    export HAS_GUI_ENABLED=''
    export HAS_SERVER_WIKI=''
    export LOOKER_BASE_URL=''
    export RLS_CAMPAIGN_ID=''
    export HAS_PRODUCT_SPEC=''
    export INTERNET_ENABLED='false'
    export LOOKER_CLIENT_ID=''
    export ENVIRONMENT_IMAGE=''
    export HAS_UI_API_CONFIG=''
    export HAS_UI_COMPONENTS=''
    export ORCHESTRATOR_MODEL=''
    export CLAUDE_PROJECTS_DIR=''
    export LOOKER_PROJECT_NAME=''
    export MCP_LOCAL_FILES_DIR=''
    export __UPPER_NAME___MODE=''
    export LOOKER_CLIENT_SECRET=''
    export OFFLINE_POSTGRES_URL=''
    export HAS_DUAL_REGISTRATION=''
    export LOOKER_WEBHOOK_SECRET=''
    export LOOKER_CONNECTION_NAME='database'
    export LOOKER_PROJECT_GIT_URL=''
    export __UPPER_NAME___API_URL=''
    export LOOKER_PROJECT_GIT_BRANCH='main'
    bash -c 'cd mcp_servers/looker && uv run --no-sync python scripts/populate_data_layer.py
# Fix ownership for biome environment (no-op if not in container)
if [ -d "/.apps_data/looker" ]; then
  chmod -R g+rw /.apps_data/looker 2>/dev/null || true
fi
'
)
echo "Populate hook completed: looker"

echo "Running populate hook for: pdfs..."
(
    cd /app/tools/mcp_servers/pdfs
    export MCP_PORT='5000'
    export APP_FS_ROOT='/filesystem'
    export MCP_TRANSPORT='http'
    export USE_INDIVIDUAL_TOOLS='true'
    export GUI_ENABLED='true'
    bash -c 'echo '"'"'No data to populate'"'"''
)
echo "Populate hook completed: pdfs"

echo "Running populate hook for: powerpoint..."
(
    cd /app/tools/mcp_servers/powerpoint
    export MCP_PORT='5000'
    export APP_FS_ROOT='/filesystem'
    export MCP_TRANSPORT='http'
    export APP_SLIDES_ROOT=''
    export USE_INDIVIDUAL_TOOLS='true'
    export GUI_ENABLED='true'
    bash -c 'echo '"'"'No data to populate'"'"''
)
echo "Populate hook completed: powerpoint"

echo "Running populate hook for: sql_execution..."
(
    cd /app/tools/mcp_servers/sql_execution
    export MCP_PORT='5000'
    export HAS_STATE='true'
    export APP_FS_ROOT='/filesystem'
    export MCP_TRANSPORT='http'
    export STATE_LOCATION='/.apps_data/sql_execution'
    export GUI_ENABLED='true'
    bash -c 'cd mcp_servers/sql_execution_server && uv run --no-sync python scripts/populate_db.py'
)
echo "Populate hook completed: sql_execution"

echo "Running populate hook for: tableau..."
(
    cd /app/tools/mcp_servers/tableau
    export LOGLEVEL=''
    export LOG_FILE=''
    export MCP_PORT='5000'
    export HAS_STATE='true'
    export JSON_LOGS=''
    export PYTHONPATH='.'
    export APP_FS_ROOT='/filesystem'
    export GUI_ENABLED='true'
    export MCP_CSV_DIR=''
    export DATABASE_URL='sqlite+aiosqlite:////.apps_data/tableau/data.db'
    export TABLEAU_MODE=''
    export MCP_TRANSPORT='http'
    export STATE_LOCATION='/.apps_data/tableau'
    export TABLEAU_API_URL=''
    export TABLEAU_SITE_ID='mercor_tableau_mcp'
    export INTERNET_ENABLED='false'
    export TABLEAU_BASE_URL=''
    export TABLEAU_TEST_MODE='local'
    export TABLEAU_AUTH_TOKEN=''
    export TABLEAU_SERVER_URL=''
    export TABLEAU_TOKEN_NAME=''
    export TABLEAU_API_VERSION=''
    export TABLEAU_DATABASE_URL='sqlite+aiosqlite:////.apps_data/tableau/data.db'
    export TABLEAU_TOKEN_SECRET=''
    export RLS_GITHUB_READ_TOKEN=''
    bash -c 'if [ -d "${STATE_LOCATION}" ] && [ "$(ls -A "${STATE_LOCATION}" 2>/dev/null)" ]; then
  echo "Importing CSV files from ${STATE_LOCATION}..."
  cd ./mcp_servers/tableau && uv run --no-sync python ../../scripts/import_csv.py --dir "${STATE_LOCATION}"
else
  echo "No CSV files to import"
fi
'
)
echo "Populate hook completed: tableau"

echo "Running populate hook for: word..."
(
    cd /app/tools/mcp_servers/word
    export MCP_PORT='5000'
    export APP_FS_ROOT='/filesystem'
    export APP_DOCS_ROOT='/docs'
    export MCP_TRANSPORT='http'
    export USE_INDIVIDUAL_TOOLS='true'
    export GUI_ENABLED='true'
    export INTERNET_ENABLED='false'
    bash -c 'echo '"'"'No data to populate'"'"''
)
echo "Populate hook completed: word"

# =============================================================================
# PHASE 6: Wait for readiness and configure apps
# =============================================================================
echo "Waiting for services to be ready..."
python3 - <<'PY'
import socket
import time
import urllib.request

PORT = 8000
MCP_PORTS = [8100,8101,8102,8103,8104,8105,8106,8107,8108]

deadline = time.time() + 120
while True:
    try:
        urllib.request.urlopen(f'http://localhost:8000/health', timeout=1).read()
        break
    except Exception:
        if time.time() > deadline:
            raise
        time.sleep(0.2)

for p in MCP_PORTS:
    deadline = time.time() + 180
    while True:
        try:
            with socket.create_connection(('127.0.0.1', p), timeout=0.5):
                break
        except Exception:
            if time.time() > deadline:
                raise RuntimeError(f'MCP port not ready: {p}')
            time.sleep(0.2)

print('Configuring MCP servers...')
data = open('/app/tools/mcp.json','rb').read()
req = urllib.request.Request(f'http://localhost:8000/apps', data=data, method='POST')
req.add_header('Content-Type','application/json')
resp = urllib.request.urlopen(req, timeout=300).read()
try:
    print(resp.decode('utf-8'))
except Exception:
    pass
print('Startup complete!')


PY

# =============================================================================
# PHASE 7: Supervise processes (fail-fast if any child exits)
# =============================================================================

set +e

EXIT_CODE=0

cleanup() {
    echo "Shutting down..."
    for pid in $CHILD_PIDS; do
        kill "$pid" 2>/dev/null || true
    done
    wait
    exit "$EXIT_CODE"
}
trap cleanup SIGTERM SIGINT

while true; do
    wait -n 2>/dev/null
    child_status=$?
    echo "A child process exited (status=$child_status), shutting down..."
    EXIT_CODE=1
    cleanup
done
