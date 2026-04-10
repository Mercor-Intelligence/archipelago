#!/usr/bin/env python3
"""
Regenerate UI for MCP servers.

Usage:
    python scripts/regenerate_ui.py --server your_server
    python scripts/regenerate_ui.py --all
    python scripts/regenerate_ui.py --server your_server --verify
"""

import argparse
import subprocess
import sys
from pathlib import Path

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from logging_config import get_logger

logger = get_logger(__name__)


def regenerate_ui_for_server(server_name: str, repo_root: Path, verify: bool = False) -> bool:
    """
    Regenerate UI for a single server.

    Args:
        server_name: Server name (e.g., "bamboohr", "auth_server")

    Returns:
        True if successful, False otherwise
    """
    server_path = repo_root / "mcp_servers" / server_name
    ui_path = repo_root / "ui" / server_name

    # Check if server has tools/ directory
    if not (server_path / "tools").exists():
        logger.info("Skipping %s: No tools/ directory found", server_name)
        return True

    logger.info("Regenerating UI for %s...", server_name)

    # Generate UI
    cmd = [
        "uv",
        "run",
        "mcp-ui-gen",
        "generate",
        "--server",
        str(server_path),
        "--output",
        str(ui_path),
        "--project-name",
        server_name,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        logger.error("Failed to generate UI for %s", server_name)
        logger.error(result.stderr)
        return False

    logger.info("Generated UI for %s", server_name)

    # Optional verification step
    if verify and ui_path.exists():
        logger.info("Verifying %s UI...", server_name)

        # Check TypeScript compilation
        package_json = ui_path / "package.json"
        if package_json.exists():
            # Install deps (silently)
            logger.info("  Installing dependencies...")
            npm_install = subprocess.run(
                ["npm", "install", "--silent"],
                cwd=ui_path,
                capture_output=True,
            )
            if npm_install.returncode != 0:
                logger.warning("npm install failed for %s", server_name)
                return False

            # Type check
            logger.info("  Type checking...")
            type_check = subprocess.run(
                ["npx", "tsc", "--noEmit"],
                cwd=ui_path,
                capture_output=True,
            )
            if type_check.returncode != 0:
                logger.warning("Type check failed for %s", server_name)
                logger.error(type_check.stderr.decode())
                return False

            logger.info("Verification passed for %s", server_name)

    return True


def main():
    parser = argparse.ArgumentParser(description="Regenerate UI for MCP servers")
    parser.add_argument(
        "--server",
        help="Server name to regenerate (e.g., your_server)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Regenerate all servers with tools/ directory",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Run npm install and type check after generation (slow)",
    )

    args = parser.parse_args()
    repo_root = Path(__file__).parent.parent

    # Determine which servers to regenerate
    servers_to_regen = []

    if args.server:
        servers_to_regen = [args.server]
    elif args.all:
        # Find all servers with tools/ directory
        servers_dir = repo_root / "mcp_servers"
        for server_dir in servers_dir.iterdir():
            if server_dir.is_dir() and (server_dir / "tools").exists():
                servers_to_regen.append(server_dir.name)
    else:
        parser.error("Must specify --server or --all")

    if not servers_to_regen:
        logger.info("No servers to regenerate")
        sys.exit(0)

    logger.info("Regenerating UI for: %s", ", ".join(servers_to_regen))

    # Regenerate each server
    success_count = 0
    fail_count = 0

    for server in servers_to_regen:
        if regenerate_ui_for_server(server, repo_root, verify=args.verify):
            success_count += 1
        else:
            fail_count += 1

    # Summary
    logger.info("=" * 60)
    logger.info("Success: %s", success_count)
    if fail_count > 0:
        logger.error("Failed: %s", fail_count)
        sys.exit(1)
    else:
        logger.info("All UIs regenerated successfully!")
        sys.exit(0)


if __name__ == "__main__":
    main()
