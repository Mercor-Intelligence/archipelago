#!/usr/bin/env python3
"""Import CSV data into database.

Implementation: mercor-mcp-shared/mcp_scripts/import_csv.py
"""

import os
import sys

# Add current working directory to sys.path so db.models can be imported
cwd = os.getcwd()
if cwd not in sys.path:
    sys.path.insert(0, cwd)

from mcp_scripts import import_csv  # noqa: E402

sys.modules[__name__] = import_csv

if __name__ == "__main__":
    import asyncio

    sys.exit(asyncio.run(import_csv.main()))
