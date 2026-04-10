#!/usr/bin/env python3
"""Validate MCP tool definitions for UI generation compatibility.

Implementation: mercor-mcp-shared/mcp_scripts/validate_mcp_tools.py
"""

import sys

from mcp_scripts import validate_mcp_tools

sys.modules[__name__] = validate_mcp_tools

if __name__ == "__main__":
    sys.exit(validate_mcp_tools.main())
