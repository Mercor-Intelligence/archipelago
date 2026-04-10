#!/usr/bin/env python3
"""Setup an MCP server for first-time use on a fresh Git clone.

This script installs all dependencies required to run the server.

Usage: setup_mcp_server.py --mcp-server <server_name>

Example: setup_mcp_server.py --mcp-server bamboohr
"""

import argparse
import subprocess
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

    parser = argparse.ArgumentParser(
        description="Setup an MCP server for first-time use on a fresh Git clone."
    )
    parser.add_argument(
        "--mcp-server",
        required=True,
        metavar="NAME",
        help=f"Name of the MCP server to setup. Available: {', '.join(available_servers)}",
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

    print(f"Setting up MCP server: {server_name}")
    print(f"Server directory: {server_dir}")
    print()

    # Step 1: Install root-level dependencies (shared packages)
    print("Installing root-level dependencies...")
    result = subprocess.run(["uv", "sync", "--all-extras"], cwd=repo_root)
    if result.returncode != 0:
        print("Error: Failed to install root-level dependencies", file=sys.stderr)
        return result.returncode

    # Step 2: Install server-specific dependencies
    print()
    print("Installing server-specific dependencies...")
    result = subprocess.run(["uv", "sync", "--all-extras"], cwd=server_dir)
    if result.returncode != 0:
        print("Error: Failed to install server dependencies", file=sys.stderr)
        return result.returncode

    print()
    print(f"Setup complete for MCP server: {server_name}")
    print()
    print("To run the server, use:")
    print(f"  scripts/run_mcp_server.py --mcp-server {server_name}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
