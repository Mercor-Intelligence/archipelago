#!/usr/bin/env python
"""Regenerate UI for MCP servers.

Implementation: mercor-mcp-shared/mcp_scripts/regenerate_ui.py
"""

import sys

from mcp_scripts import regenerate_ui

sys.modules[__name__] = regenerate_ui

if __name__ == "__main__":
    sys.exit(regenerate_ui.main())
