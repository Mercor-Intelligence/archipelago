#!/usr/bin/env python3
"""Validate that all MCP tool names use lowercase + underscores only.

Implementation: mercor-mcp-shared/mcp_scripts/validate_tool_names.py
"""

import sys

from mcp_scripts import validate_tool_names

sys.modules[__name__] = validate_tool_names

if __name__ == "__main__":
    sys.exit(validate_tool_names.main())
