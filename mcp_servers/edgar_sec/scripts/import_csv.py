#!/usr/bin/env python
"""CSV to Database Import Script

Implementation: mercor-mcp-shared/mcp_scripts/import_csv.py
"""

import asyncio
import os
import sys

from mcp_scripts import import_csv

sys.modules[__name__] = import_csv

if __name__ == "__main__":
    # Add CWD to sys.path so `from db.models import Base` works when the
    # lifecycle hook cd's into the MCP server directory before running this script.
    cwd = os.getcwd()
    if cwd not in sys.path:
        sys.path.insert(0, cwd)
    sys.exit(asyncio.run(import_csv.main()))
