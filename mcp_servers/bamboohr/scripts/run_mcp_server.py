#!/usr/bin/env python3
"""Run an MCP server in STDIO mode.

Usage: run_mcp_server.py --mcp-server <server_name>

Example: run_mcp_server.py --mcp-server bamboohr
"""

import argparse
import os
import sys
from pathlib import Path


def get_repo_root() -> Path:
    """Get the repository root directory."""
    return Path(__file__).parent.parent.resolve()


def get_available_servers(mcp_servers_dir: Path) -> list[str]:
    """Get list of available MCP servers (directories with main.py)."""
    servers = []
    for server_dir in sorted(mcp_servers_dir.iterdir()):
        if server_dir.is_dir() and (server_dir / "main.py").exists():
            servers.append(server_dir.name)
    return servers


def main() -> int:
    repo_root = get_repo_root()
    mcp_servers_dir = repo_root / "mcp_servers"
    available_servers = get_available_servers(mcp_servers_dir)

    parser = argparse.ArgumentParser(description="Run an MCP server in STDIO mode.")
    parser.add_argument(
        "--mcp-server",
        required=True,
        metavar="NAME",
        help=f"Name of the MCP server to run. Available: {', '.join(available_servers)}",
    )
    args = parser.parse_args()

    server_name = args.mcp_server
    server_dir = mcp_servers_dir / server_name

    # Validate server exists
    if not server_dir.is_dir():
        print(f"Error: Server '{server_name}' not found at {server_dir}", file=sys.stderr)
        print("\nAvailable servers:", file=sys.stderr)
        for server in available_servers:
            print(f"  - {server}", file=sys.stderr)
        return 1

    if not (server_dir / "main.py").exists():
        print(f"Error: Server '{server_name}' is missing main.py", file=sys.stderr)
        return 1

    # Add server directory to sys.path and change working directory
    # This is the single sys.path manipulation point for running servers
    sys.path.insert(0, str(server_dir))
    os.chdir(server_dir)

    # Import and run the server via main() which includes logging configuration
    from main import main as server_main

    server_main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
