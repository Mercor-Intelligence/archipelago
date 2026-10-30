#!/usr/bin/env python
"""Generate a new MCP server from template.

Implementation: mercor-mcp-shared/mcp_scripts/create_mcp_server.py
"""

import sys

from mcp_scripts import create_mcp_server

sys.modules[__name__] = create_mcp_server

if __name__ == "__main__":
    sys.exit(create_mcp_server.main())
