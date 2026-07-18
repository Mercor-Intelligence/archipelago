#!/bin/bash
#
# claude-logs-watch.sh
# Watches Claude Code log directory and automatically copies new transcript files to current directory
#
# This is a lightweight alternative that doesn't require Docker or OTLP setup.
# It simply monitors the Claude projects directory and copies new .jsonl files.
#
# Usage:
#   ./claude-logs-watch.sh [--output-dir DIR] [--daemon]
#

set -euo pipefail

# Configuration
OUTPUT_DIR=".claude-audit"
DAEMON_MODE=false
PID_FILE="/tmp/claude-logs-watch.pid"

# Detect OS
if [[ "$OSTYPE" == "darwin"* ]]; then
    CLAUDE_PROJECTS_DIR="$HOME/.claude/projects"
    WATCH_CMD="fswatch"
elif [[ "$OSTYPE" == "linux-gnu"* ]]; then
    CLAUDE_PROJECTS_DIR="$HOME/.claude/projects"
    WATCH_CMD="inotifywait"
else
    echo "Unsupported OS: $OSTYPE"
    exit 1
fi

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --output-dir)
            OUTPUT_DIR="$2"
            shift 2
            ;;
        --daemon)
            DAEMON_MODE=true
            shift
            ;;
        --stop)
            if [[ -f "$PID_FILE" ]]; then
                kill $(cat "$PID_FILE") 2>/dev/null && echo "Stopped watcher" || echo "Watcher not running"
                rm -f "$PID_FILE"
            else
                echo "No PID file found"
            fi
            exit 0
            ;;
        --help)
            grep '^#' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}"
            exit 1
            ;;
    esac
done

# Create output directory
mkdir -p "$OUTPUT_DIR"

# Check if watch command is available
check_dependencies() {
    if [[ "$WATCH_CMD" == "fswatch" ]] && ! command -v fswatch &> /dev/null; then
        echo -e "${RED}Error: fswatch not found${NC}"
        echo "Install with: brew install fswatch"
        exit 1
    elif [[ "$WATCH_CMD" == "inotifywait" ]] && ! command -v inotifywait &> /dev/null; then
        echo -e "${RED}Error: inotifywait not found${NC}"
        echo "Install with: sudo apt-get install inotify-tools"
        exit 1
    fi
}

# Function to copy a file
copy_file() {
    local src="$1"
    local filename=$(basename "$src")
    local dest="$OUTPUT_DIR/$filename"

    # Extract session info if possible
    local session_info=""
    if command -v jq &> /dev/null; then
        session_info=$(head -n 1 "$src" 2>/dev/null | jq -r '.session_id // empty' 2>/dev/null || echo "")
    fi

    if [[ ! -f "$dest" ]] || [[ "$src" -nt "$dest" ]]; then
        cp "$src" "$dest"
        local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
        echo "[$timestamp] Copied: $filename ${session_info:+(Session: $session_info)}"

        # Create a human-readable summary if jq is available
        if command -v jq &> /dev/null && [[ -f "$dest" ]]; then
            create_summary "$dest"
        fi
    fi
}

# Create a quick summary file
create_summary() {
    local jsonl_file="$1"
    local summary_file="${jsonl_file%.jsonl}-summary.txt"

    {
        echo "=== Claude Code Session Summary ==="
        echo "Generated: $(date)"
        echo ""

        # Extract session metadata
        local first_line=$(head -n 1 "$jsonl_file")
        echo "Session ID: $(echo "$first_line" | jq -r '.session_id // "N/A"')"
        echo "Start Time: $(echo "$first_line" | jq -r '.timestamp // "N/A"')"
        echo "Working Directory: $(echo "$first_line" | jq -r '.working_directory // "N/A"')"
        echo ""

        # Count interactions
        local user_messages=$(grep -c '"type":"user_message"' "$jsonl_file" 2>/dev/null || echo "0")
        local tool_uses=$(grep -c '"type":"tool_use"' "$jsonl_file" 2>/dev/null || echo "0")
        echo "User Messages: $user_messages"
        echo "Tool Uses: $tool_uses"
        echo ""

        # List files modified (if any)
        echo "Files Modified:"
        grep '"type":"tool_use"' "$jsonl_file" 2>/dev/null | \
            jq -r 'select(.input.path != null) | .input.path' 2>/dev/null | \
            sort -u | \
            sed 's/^/  - /' || echo "  (none detected)"

    } > "$summary_file" 2>/dev/null
}

# Watch function using fswatch (macOS)
watch_fswatch() {
    echo -e "${GREEN}Watching: $CLAUDE_PROJECTS_DIR${NC}"
    echo -e "${GREEN}Output directory: $OUTPUT_DIR${NC}"
    echo ""
    echo "Press Ctrl+C to stop..."
    echo ""

    # Do initial sync
    find "$CLAUDE_PROJECTS_DIR" -name "*.jsonl" -type f 2>/dev/null | while read -r file; do
        copy_file "$file"
    done

    # Watch for changes
    fswatch -0 -r -e ".*" -i "\\.jsonl$" "$CLAUDE_PROJECTS_DIR" | while read -d "" file; do
        copy_file "$file"
    done
}

# Watch function using inotifywait (Linux)
watch_inotifywait() {
    echo -e "${GREEN}Watching: $CLAUDE_PROJECTS_DIR${NC}"
    echo -e "${GREEN}Output directory: $OUTPUT_DIR${NC}"
    echo ""
    echo "Press Ctrl+C to stop..."
    echo ""

    # Do initial sync
    find "$CLAUDE_PROJECTS_DIR" -name "*.jsonl" -type f 2>/dev/null | while read -r file; do
        copy_file "$file"
    done

    # Watch for changes
    inotifywait -m -r -e close_write,moved_to --format '%w%f' "$CLAUDE_PROJECTS_DIR" | while read -r file; do
        if [[ "$file" == *.jsonl ]]; then
            copy_file "$file"
        fi
    done
}

# Main execution
main() {
    echo -e "${GREEN}=== Claude Code Log Watcher ===${NC}"
    echo ""

    # Check dependencies
    check_dependencies

    # Check if Claude directory exists
    if [[ ! -d "$CLAUDE_PROJECTS_DIR" ]]; then
        echo -e "${RED}Error: Claude projects directory not found: $CLAUDE_PROJECTS_DIR${NC}"
        echo "Make sure you've used Claude Code at least once."
        exit 1
    fi

    # Daemon mode
    if [[ "$DAEMON_MODE" == true ]]; then
        echo "Starting in daemon mode..."
        nohup "$0" --output-dir "$OUTPUT_DIR" > "$OUTPUT_DIR/watcher.log" 2>&1 &
        echo $! > "$PID_FILE"
        echo -e "${GREEN}✓${NC} Watcher started in background (PID: $(cat "$PID_FILE"))"
        echo "  Stop with: $0 --stop"
        echo "  View logs: tail -f $OUTPUT_DIR/watcher.log"
        exit 0
    fi

    # Start watching
    if [[ "$WATCH_CMD" == "fswatch" ]]; then
        watch_fswatch
    else
        watch_inotifywait
    fi
}

# Handle Ctrl+C gracefully
trap 'echo ""; echo "Stopped watching"; exit 0' INT

main
