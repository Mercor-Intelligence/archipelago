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

echo "Starting MCP server: bloomberg on port 8100..."
(
    cd /app/tools/mcp_servers/bloomberg
    export MCP_PORT=8100
    export MCP_TRANSPORT=http
    export MODE='offline'
    export TOOLS=''
    export SRC_DIR=''
    export DEST_DIR=''
    export ENV_FILE='.env.local'
    export USE_MOCK='false'
    export HAS_STATE='true'
    export PYTHONPATH='.'
    export APP_FS_ROOT='/filesystem'
    export DUCKDB_PATH='data/offline.duckdb'
    export GUI_ENABLED='true'
    export MOCK_OPENBB='false'
    export DATABASE_URL=''
    export MCP_TRANSPORT='http'
    export STATE_LOCATION='/.apps_data/bloomberg'
    export OPENBB_DATA_DIR=''
    export INTERNET_ENABLED='false'
    : "${FMP_API_KEY:?}"
    exec bash -c 'uv run --no-sync python -m mcp_servers.bloomberg.main'
) &
CHILD_PIDS="$CHILD_PIDS $!"

echo "Starting MCP server: calendar on port 8101..."
(
    cd /app/tools/mcp_servers/calendar
    export MCP_PORT=8101
    export MCP_TRANSPORT=http
    export HAS_STATE='true'
    export APP_FS_ROOT='/filesystem'
    export MCP_TRANSPORT='http'
    export STATE_LOCATION='/.apps_data/calendar'
    export INTERNET_ENABLED='false'
    export APP_APPS_DATA_ROOT='/.apps_data'
    export USE_INDIVIDUAL_TOOLS='true'
    export APP_CALENDAR_DATA_ROOT='/.apps_data/calendar'
    export APP_CALENDAR_LIST_MAX_LIMIT='100'
    export APP_CALENDAR_LIST_DEFAULT_LIMIT='50'
    exec bash -c 'cd mcp_servers/calendar_server && uv run --no-sync python main.py'
) &
CHILD_PIDS="$CHILD_PIDS $!"

echo "Starting MCP server: chat on port 8102..."
(
    cd /app/tools/mcp_servers/chat
    export MCP_PORT=8102
    export MCP_TRANSPORT=http
    export HAS_STATE='true'
    export APP_FS_ROOT='/filesystem'
    export MCP_TRANSPORT='http'
    export STATE_LOCATION='/.apps_data/chat'
    export APP_APPS_DATA_ROOT='/.apps_data'
    export USE_INDIVIDUAL_TOOLS='true'
    exec bash -c 'cd mcp_servers/chat_server && uv run --no-sync python main.py'
) &
CHILD_PIDS="$CHILD_PIDS $!"

echo "Starting MCP server: code_execution on port 8103..."
(
    cd /app/tools/mcp_servers/code_execution
    export MCP_PORT=8103
    export MCP_TRANSPORT=http
    export APP_FS_ROOT='/filesystem'
    export MCP_TRANSPORT='http'
    export SANDBOX_LIBRARY_PATH='/app/lib/sandbox_fs.so'
    export CODE_EXEC_COMMAND_TIMEOUT='300'
    exec bash -c 'cd mcp_servers/code_execution_server && uv run --no-sync python main.py'
) &
CHILD_PIDS="$CHILD_PIDS $!"

echo "Starting MCP server: edgar_sec on port 8104..."
(
    cd /app/tools/mcp_servers/edgar_sec
    export MCP_PORT=8104
    export MCP_TRANSPORT=http
    export HAS_STATE='true'
    export APP_FS_ROOT='/filesystem'
    export GUI_ENABLED='true'
    export MCP_TRANSPORT='http'
    export STATE_LOCATION='/.apps_data/edgar_sec'
    export EDGAR_USER_AGENT='RL-Studio rls@mercor.com'
    export INTERNET_ENABLED='false'
    export EDGAR_OFFLINE_MODE='true'
    exec bash -c 'cd mcp_servers/edgar_sec && uv run --no-sync python main.py'
) &
CHILD_PIDS="$CHILD_PIDS $!"

echo "Starting MCP server: excel on port 8105..."
(
    cd /app/tools/mcp_servers/excel
    export MCP_PORT=8105
    export MCP_TRANSPORT=http
    export APP_FS_ROOT='/filesystem'
    export MCP_TRANSPORT='http'
    export APP_SHEETS_ROOT=''
    export LIBREOFFICE_TIMEOUT='30'
    export SKIP_FORMULA_RECALC='false'
    export USE_INDIVIDUAL_TOOLS='true'
    exec bash -c 'cd mcp_servers/sheets_server && uv run --no-sync python main.py'
) &
CHILD_PIDS="$CHILD_PIDS $!"

echo "Starting MCP server: filesystem on port 8106..."
(
    cd /app/tools/mcp_servers/filesystem
    export MCP_PORT=8106
    export MCP_TRANSPORT=http
    export APP_FS_ROOT='/filesystem'
    export MCP_TRANSPORT='http'
    exec bash -c 'cd mcp_servers/filesystem_server && uv run --no-sync python main.py'
) &
CHILD_PIDS="$CHILD_PIDS $!"

echo "Starting MCP server: fmp on port 8107..."
(
    cd /app/tools/mcp_servers/fmp
    export MCP_PORT=8107
    export MCP_TRANSPORT=http
    export HAS_STATE='true'
    export APP_FS_ROOT='/filesystem'
    export GUI_ENABLED='true'
    export MCP_TRANSPORT='http'
    export STATE_LOCATION='/.apps_data/fmp'
    export XERO_CLIENT_ID=''
    export FMP_OFFLINE_MODE='true'
    export INTERNET_ENABLED='false'
    : "${FMP_API_KEY:?}"
    exec bash -c 'cd mcp_servers/fmp_server && uv run --no-sync python main.py'
) &
CHILD_PIDS="$CHILD_PIDS $!"

echo "Starting MCP server: mail on port 8108..."
(
    cd /app/tools/mcp_servers/mail
    export MCP_PORT=8108
    export MCP_TRANSPORT=http
    export HAS_STATE='true'
    export APP_FS_ROOT='/filesystem'
    export MCP_TRANSPORT='http'
    export STATE_LOCATION='/.apps_data/mail'
    export APP_APPS_DATA_ROOT='/.apps_data'
    export USE_INDIVIDUAL_TOOLS='true'
    exec bash -c 'cd mcp_servers/mail_server && uv run --no-sync python main.py'
) &
CHILD_PIDS="$CHILD_PIDS $!"

echo "Starting MCP server: pdfs on port 8109..."
(
    cd /app/tools/mcp_servers/pdfs
    export MCP_PORT=8109
    export MCP_TRANSPORT=http
    export APP_FS_ROOT='/filesystem'
    export MCP_TRANSPORT='http'
    export USE_INDIVIDUAL_TOOLS='true'
    exec bash -c 'cd mcp_servers/pdf_server && uv run --no-sync python main.py'
) &
CHILD_PIDS="$CHILD_PIDS $!"

echo "Starting MCP server: powerpoint on port 8110..."
(
    cd /app/tools/mcp_servers/powerpoint
    export MCP_PORT=8110
    export MCP_TRANSPORT=http
    export APP_FS_ROOT='/filesystem'
    export MCP_TRANSPORT='http'
    export APP_SLIDES_ROOT=''
    export USE_INDIVIDUAL_TOOLS='true'
    exec bash -c 'cd mcp_servers/slides_server && uv run --no-sync python main.py'
) &
CHILD_PIDS="$CHILD_PIDS $!"

echo "Starting MCP server: terrapin on port 8111..."
(
    cd /app/tools/mcp_servers/terrapin
    export MCP_PORT=8111
    export MCP_TRANSPORT=http
    export HAS_STATE='true'
    export APP_FS_ROOT='/filesystem'
    export GUI_ENABLED='true'
    export DATABASE_URL=''
    export MCP_TRANSPORT='http'
    export STATE_LOCATION='/.apps_data/terrapin'
    export INTERNET_ENABLED='false'
    export TERRAPIN_OFFLINE='1'
    export NEXT_PUBLIC_API_BASE=''
    export TERRAPIN_API_BASE_URL=''
    : "${TERRAPIN_API_KEY:?}"
    exec bash -c 'cd mcp_servers/terrapin && uv run --no-sync python main.py'
) &
CHILD_PIDS="$CHILD_PIDS $!"

echo "Starting MCP server: word on port 8112..."
(
    cd /app/tools/mcp_servers/word
    export MCP_PORT=8112
    export MCP_TRANSPORT=http
    export APP_FS_ROOT='/filesystem'
    export APP_DOCS_ROOT='/docs'
    export MCP_TRANSPORT='http'
    export USE_INDIVIDUAL_TOOLS='true'
    exec bash -c 'cd mcp_servers/docs_server && uv run --no-sync python main.py'
) &
CHILD_PIDS="$CHILD_PIDS $!"

# =============================================================================
# PHASE 5: Run populate hooks (database initialization, etc.)
# =============================================================================
echo "Running populate hook for: bloomberg..."
(
    cd /app/tools/mcp_servers/bloomberg
    export MODE='offline'
    export TOOLS=''
    export SRC_DIR=''
    export DEST_DIR=''
    export ENV_FILE='.env.local'
    export MCP_PORT='5000'
    export USE_MOCK='false'
    export HAS_STATE='true'
    export PYTHONPATH='.'
    export APP_FS_ROOT='/filesystem'
    export DUCKDB_PATH='data/offline.duckdb'
    export GUI_ENABLED='true'
    export MOCK_OPENBB='false'
    export DATABASE_URL=''
    export MCP_TRANSPORT='http'
    export STATE_LOCATION='/.apps_data/bloomberg'
    export OPENBB_DATA_DIR=''
    export INTERNET_ENABLED='false'
    : "${FMP_API_KEY:?}"
    bash -c 'mkdir -p "${STATE_LOCATION}" data && count=$(ls "${STATE_LOCATION}"/*.duckdb 2>/dev/null | wc -l) && if [ "$count" -gt 1 ]; then echo "Error: expected at most 1 .duckdb file but found $count" >&2; exit 1; elif [ "$count" -eq 1 ]; then cp "${STATE_LOCATION}"/*.duckdb data/offline.duckdb; fi
'
)
echo "Populate hook completed: bloomberg"

echo "Running populate hook for: code_execution..."
(
    cd /app/tools/mcp_servers/code_execution
    export MCP_PORT='5000'
    export APP_FS_ROOT='/filesystem'
    export MCP_TRANSPORT='http'
    export SANDBOX_LIBRARY_PATH='/app/lib/sandbox_fs.so'
    export CODE_EXEC_COMMAND_TIMEOUT='300'
    bash -c 'echo '"'"'No data to populate'"'"''
)
echo "Populate hook completed: code_execution"

echo "Running populate hook for: edgar_sec..."
(
    cd /app/tools/mcp_servers/edgar_sec
    export MCP_PORT='5000'
    export HAS_STATE='true'
    export APP_FS_ROOT='/filesystem'
    export GUI_ENABLED='true'
    export MCP_TRANSPORT='http'
    export STATE_LOCATION='/.apps_data/edgar_sec'
    export EDGAR_USER_AGENT='RL-Studio rls@mercor.com'
    export INTERNET_ENABLED='false'
    export EDGAR_OFFLINE_MODE='true'
    bash -c 'mkdir -p "${STATE_LOCATION}" data && count=$(ls "${STATE_LOCATION}"/*.zip 2>/dev/null | wc -l) && if [ "$count" -gt 1 ]; then echo "Error: expected at most 1 .zip file but found $count" >&2; exit 1; elif [ "$count" -eq 1 ]; then cp "${STATE_LOCATION}"/*.zip data/edgar_offline.zip; fi && if [ -f data/edgar_offline.zip ]; then python -m zipfile -e data/edgar_offline.zip ./offline_data && echo "Extracted edgar_offline.zip to ./offline_data/"; fi
'
)
echo "Populate hook completed: edgar_sec"

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
    bash -c 'echo '"'"'No data to populate'"'"''
)
echo "Populate hook completed: excel"

echo "Running populate hook for: filesystem..."
(
    cd /app/tools/mcp_servers/filesystem
    export MCP_PORT='5000'
    export APP_FS_ROOT='/filesystem'
    export MCP_TRANSPORT='http'
    bash -c 'echo '"'"'No data to populate'"'"''
)
echo "Populate hook completed: filesystem"

echo "Running populate hook for: fmp..."
(
    cd /app/tools/mcp_servers/fmp
    export MCP_PORT='5000'
    export HAS_STATE='true'
    export APP_FS_ROOT='/filesystem'
    export GUI_ENABLED='true'
    export MCP_TRANSPORT='http'
    export STATE_LOCATION='/.apps_data/fmp'
    export XERO_CLIENT_ID=''
    export FMP_OFFLINE_MODE='true'
    export INTERNET_ENABLED='false'
    : "${FMP_API_KEY:?}"
    bash -c 'mkdir -p "${STATE_LOCATION}" data mcp_servers/fmp_server/data && count=$(ls "${STATE_LOCATION}"/*.db 2>/dev/null | wc -l) && if [ "$count" -gt 1 ]; then echo "Error: expected at most 1 .db file but found $count" >&2; exit 1; elif [ "$count" -eq 1 ]; then cp "${STATE_LOCATION}"/*.db mcp_servers/fmp_server/data/fmp.db && echo "Copied fmp.db to mcp_servers/fmp_server/data/"; fi
'
)
echo "Populate hook completed: fmp"

echo "Running populate hook for: pdfs..."
(
    cd /app/tools/mcp_servers/pdfs
    export MCP_PORT='5000'
    export APP_FS_ROOT='/filesystem'
    export MCP_TRANSPORT='http'
    export USE_INDIVIDUAL_TOOLS='true'
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
    bash -c 'echo '"'"'No data to populate'"'"''
)
echo "Populate hook completed: powerpoint"

echo "Running populate hook for: terrapin..."
(
    cd /app/tools/mcp_servers/terrapin
    export MCP_PORT='5000'
    export HAS_STATE='true'
    export APP_FS_ROOT='/filesystem'
    export GUI_ENABLED='true'
    export DATABASE_URL=''
    export MCP_TRANSPORT='http'
    export STATE_LOCATION='/.apps_data/terrapin'
    export INTERNET_ENABLED='false'
    export TERRAPIN_OFFLINE='1'
    export NEXT_PUBLIC_API_BASE=''
    export TERRAPIN_API_BASE_URL=''
    : "${TERRAPIN_API_KEY:?}"
    bash -c 'mkdir -p "${STATE_LOCATION}" mcp_servers/terrapin/fixtures && count=$(ls "${STATE_LOCATION}"/*.duckdb 2>/dev/null | wc -l) && if [ "$count" -gt 1 ]; then echo "Error: expected at most 1 .duckdb file but found $count" >&2; exit 1; elif [ "$count" -eq 1 ]; then cp "${STATE_LOCATION}"/*.duckdb mcp_servers/terrapin/fixtures/fixtures.duckdb; fi
'
)
echo "Populate hook completed: terrapin"

echo "Running populate hook for: word..."
(
    cd /app/tools/mcp_servers/word
    export MCP_PORT='5000'
    export APP_FS_ROOT='/filesystem'
    export APP_DOCS_ROOT='/docs'
    export MCP_TRANSPORT='http'
    export USE_INDIVIDUAL_TOOLS='true'
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
MCP_PORTS = [8100,8101,8102,8103,8104,8105,8106,8107,8108,8109,8110,8111,8112]

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
