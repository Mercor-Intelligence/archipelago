#!/usr/bin/env python3
"""CSV to Database Import Script.

Implementation: mercor-mcp-shared/mcp_scripts/import_csv.py
"""

import asyncio
import sys
from pathlib import Path

# Add the MCP server directory to sys.path so that import_csv.py can find db.models
sys.path.insert(0, str(Path(__file__).parent.parent / "mcp_servers" / "workday"))

from mcp_scripts import import_csv

sys.modules[__name__] = import_csv

if __name__ == "__main__":
    asyncio.run(import_csv.main())
