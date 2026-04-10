#!/usr/bin/env python3
"""Generic Database Management Tools for MCP Servers.

Implementation: mercor-mcp-shared/packages/mcp_middleware/mcp_middleware/db_tools.py
"""

import sys

from mcp_middleware import db_tools

sys.modules[__name__] = db_tools
