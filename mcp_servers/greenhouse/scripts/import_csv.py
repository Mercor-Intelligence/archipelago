#!/usr/bin/env python3
"""CSV import/validation with full schema awareness.

Implementation: mercor-mcp-shared/mcp_scripts/import_csv.py
"""

import asyncio
import sys
from pathlib import Path

# Set up paths before importing from mcp_scripts
_project_root = Path(__file__).parent.parent
_mcp_server_path = _project_root / "mcp_servers" / "greenhouse"
sys.path.insert(0, str(_project_root))
sys.path.insert(0, str(_mcp_server_path))

from mcp_scripts import import_csv  # noqa: E402

sys.modules[__name__] = import_csv

if __name__ == "__main__":
    asyncio.run(import_csv.main())
