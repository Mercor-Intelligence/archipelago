#!/usr/bin/env python3
"""Run local UI development environment.

Implementation: mercor-mcp-shared/mcp_scripts/run_local_ui.py
"""

import sys

from mcp_scripts import run_local_ui

sys.modules[__name__] = run_local_ui

if __name__ == "__main__":
    sys.exit(run_local_ui.main())
