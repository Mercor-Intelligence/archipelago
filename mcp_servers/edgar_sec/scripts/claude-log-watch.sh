#!/bin/bash
# Claude Code log watcher.
#
# Implementation: mercor-mcp-shared/mcp_scripts/claude-log-watch.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/../packages/mercor-mcp-shared/mcp_scripts/claude-log-watch.sh" "$@"
