#!/bin/bash
# Start local UI development environment
# Installs mercor-mcp from git and runs mcp-ui

uv pip install "mercor-mcp @ git+https://github.com/Mercor-Intelligence/mercor-mcp.git@main" -q
uv run mcp-ui "$@"
