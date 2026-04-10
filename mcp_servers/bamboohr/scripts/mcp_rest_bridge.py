#!/usr/bin/env python3
"""MCP REST Bridge - HTTP API for MCP servers.

Implementation: mercor-mcp-shared/mcp_scripts/mcp_rest_bridge.py
"""

import sys

from mcp_scripts import mcp_rest_bridge

sys.modules[__name__] = mcp_rest_bridge

if __name__ == "__main__":
    sys.exit(mcp_rest_bridge.main())
