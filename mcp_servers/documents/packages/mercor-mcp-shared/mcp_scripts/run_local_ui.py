#!/usr/bin/env python3
"""
Run the local UI development environment.

This script starts both the REST bridge and the Next.js UI in one command.

Implementation: mercor-mcp-shared/ui_generator/cli/run_ui.py

Usage:
    python scripts/run_local_ui.py --server <server_name>
    python scripts/run_local_ui.py --server looker --port 8000 --ui-port 3000
    python scripts/run_local_ui.py -e mcp_servers.example.ui  # explicit entrypoint
"""

import sys

# Re-export for backwards compatibility
from ui_generator.cli.run_ui import (
    run_ui,
)


def main():
    """Main entry point - delegates to run_ui click command."""
    return run_ui(standalone_mode=False)


if __name__ == "__main__":
    sys.exit(run_ui())
