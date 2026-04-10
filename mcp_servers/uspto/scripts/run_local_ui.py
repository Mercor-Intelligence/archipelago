#!/usr/bin/env python3
"""
Run the local UI development environment.

This script starts both the REST bridge and the Next.js UI in one command.

Usage:
    python scripts/run_local_ui.py --server <server_name>
    python scripts/run_local_ui.py --server looker --port 8000 --ui-port 3000
"""

import argparse
import os
import signal
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from logging_config import get_logger

logger = get_logger(__name__)


def check_npm_installed():
    """Check if npm is installed."""
    try:
        subprocess.run(["npm", "--version"], capture_output=True, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def find_server(repo_root: Path, server_name: str) -> Path | None:
    """Find the server directory."""
    # Check mcp_servers/<name>
    server_dir = repo_root / "mcp_servers" / server_name
    if server_dir.exists():
        return server_dir

    # Check src/mcp_servers/<name>
    server_dir = repo_root / "src" / "mcp_servers" / server_name
    if server_dir.exists():
        return server_dir

    return None


def find_ui(repo_root: Path, server_name: str) -> Path | None:
    """Find the UI directory."""
    ui_dir = repo_root / "ui" / server_name
    if ui_dir.exists():
        return ui_dir
    return None


def get_mcp_module(repo_root: Path, server_name: str) -> str | None:
    """Get the MCP module path for the server (prefers ui.py for REST bridge)."""
    # Prefer ui.py for GUI/REST bridge consumption
    if (repo_root / "mcp_servers" / server_name / "ui.py").exists():
        return f"mcp_servers.{server_name}.ui"
    if (repo_root / "src" / "mcp_servers" / server_name / "ui.py").exists():
        return f"src.mcp_servers.{server_name}.ui"

    # Fall back to main.py if ui.py doesn't exist
    if (repo_root / "mcp_servers" / server_name / "main.py").exists():
        return f"mcp_servers.{server_name}.main"
    if (repo_root / "src" / "mcp_servers" / server_name / "main.py").exists():
        return f"src.mcp_servers.{server_name}.main"

    return None


def list_available_servers(repo_root: Path) -> list[str]:
    """List available servers (checks for ui.py or main.py)."""
    servers = []

    # Check mcp_servers/
    mcp_dir = repo_root / "mcp_servers"
    if mcp_dir.exists():
        for d in mcp_dir.iterdir():
            if d.is_dir() and ((d / "ui.py").exists() or (d / "main.py").exists()):
                servers.append(d.name)

    # Check src/mcp_servers/
    src_mcp_dir = repo_root / "src" / "mcp_servers"
    if src_mcp_dir.exists():
        for d in src_mcp_dir.iterdir():
            if d.is_dir() and ((d / "ui.py").exists() or (d / "main.py").exists()):
                servers.append(d.name)

    return sorted(set(servers))


def main():
    parser = argparse.ArgumentParser(description="Run local UI development environment")
    parser.add_argument(
        "--server",
        "-s",
        help="Server name (e.g., looker, xero). Required unless only one server exists.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port for REST bridge (default: 8000)",
    )
    parser.add_argument(
        "--ui-port",
        type=int,
        default=3000,
        help="Port for Next.js UI (default: 3000)",
    )
    parser.add_argument(
        "--skip-install",
        action="store_true",
        help="Skip npm install (faster if dependencies already installed)",
    )
    parser.add_argument(
        "--regenerate",
        "-r",
        action="store_true",
        help="Regenerate UI before starting",
    )
    parser.add_argument(
        "--list",
        "-l",
        action="store_true",
        help="List available servers",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Don't auto-open browser",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).parent.parent

    # List servers if requested
    if args.list:
        servers = list_available_servers(repo_root)
        if servers:
            logger.info("Available servers:")
            for s in servers:
                ui_exists = "  [UI exists]" if find_ui(repo_root, s) else ""
                logger.info("  - %s%s", s, ui_exists)
        else:
            logger.info("No servers found")
        sys.exit(0)

    # Auto-detect server if not specified
    server_name = args.server
    if not server_name:
        servers = list_available_servers(repo_root)
        if len(servers) == 0:
            logger.error("No servers found in mcp_servers/")
            sys.exit(1)
        elif len(servers) == 1:
            server_name = servers[0]
            logger.info("Auto-detected server: %s", server_name)
        else:
            # Multiple servers - prefer one that already has a UI
            servers_with_ui = [s for s in servers if find_ui(repo_root, s)]
            if len(servers_with_ui) == 1:
                server_name = servers_with_ui[0]
                logger.info("Auto-detected server (has UI): %s", server_name)
            elif len(servers_with_ui) > 1:
                logger.error("Multiple servers with UIs found. Please specify --server:")
                for s in servers_with_ui:
                    logger.error("  - %s", s)
                sys.exit(1)
            else:
                logger.error("Multiple servers found, none have UIs. Please specify --server:")
                for s in servers:
                    logger.error("  - %s", s)
                sys.exit(1)

    # Find server and UI directories
    server_dir = find_server(repo_root, server_name)
    if not server_dir:
        logger.error("Server '%s' not found", server_name)
        sys.exit(1)

    ui_dir = find_ui(repo_root, server_name)
    mcp_module = get_mcp_module(repo_root, server_name)

    if not mcp_module:
        logger.error("Could not find ui.py or main.py for server '%s'", server_name)
        sys.exit(1)

    # Regenerate UI if requested or if UI doesn't exist
    if args.regenerate or not ui_dir:
        logger.info("Regenerating UI for %s...", server_name)
        regen_script = str(repo_root / "scripts" / "regenerate_ui.py")
        result = subprocess.run(
            [sys.executable, regen_script, "--server", server_name],
            cwd=repo_root,
        )
        if result.returncode != 0:
            logger.error("Failed to regenerate UI")
            sys.exit(1)
        ui_dir = find_ui(repo_root, server_name)

    if not ui_dir or not ui_dir.exists():
        logger.error("UI directory not found at ui/%s", server_name)
        logger.error("Run with --regenerate to generate the UI first")
        sys.exit(1)

    if not check_npm_installed():
        logger.error("npm is not installed. Please install Node.js first.")
        sys.exit(1)

    processes = []

    def cleanup(signum=None, frame=None, exit_code=0):
        """Clean up child processes."""
        logger.info("Shutting down...")
        for proc in processes:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        sys.exit(exit_code)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    # Install npm dependencies if needed
    if not args.skip_install:
        node_modules = ui_dir / "node_modules"
        if not node_modules.exists():
            logger.info("Installing npm dependencies...")
            result = subprocess.run(
                ["npm", "install"],
                cwd=ui_dir,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                logger.error("Error installing dependencies: %s", result.stderr)
                sys.exit(1)
            logger.info("Dependencies installed.")

    # Start REST bridge
    logger.info("Starting REST bridge on http://127.0.0.1:%s", args.port)
    bridge_proc = subprocess.Popen(
        [
            sys.executable,
            str(repo_root / "scripts" / "mcp_rest_bridge.py"),
            "--mcp-server",
            mcp_module,
            "--port",
            str(args.port),
        ],
        cwd=repo_root,
        env={**os.environ, "PYTHONPATH": str(repo_root)},
    )
    processes.append(bridge_proc)

    # Wait for bridge to start
    time.sleep(2)

    if bridge_proc.poll() is not None:
        logger.error("REST bridge failed to start")
        cleanup(exit_code=1)

    # Start Next.js UI
    logger.info("Starting Next.js UI on http://localhost:%s", args.ui_port)
    api_base = f"http://127.0.0.1:{args.port}"
    # Capitalize server name for display (e.g., looker -> Looker)
    display_name = server_name.replace("_", " ").title()
    ui_env = {
        **os.environ,
        "PORT": str(args.ui_port),
        "BUILD_MODE": "local",
        "NEXT_PUBLIC_API_BASE": api_base,
        "NEXT_PUBLIC_API_URL": api_base,  # Legacy support
        "NEXT_PUBLIC_SERVER_NAME": display_name,
    }
    ui_proc = subprocess.Popen(
        ["npm", "run", "dev"],
        cwd=ui_dir,
        env=ui_env,
    )
    processes.append(ui_proc)

    # Wait for Next.js to be ready, then open browser
    time.sleep(3)

    ui_url = f"http://localhost:{args.ui_port}"
    logger.info("=" * 60)
    logger.info("  Server: %s", server_name)
    logger.info("  UI: %s", ui_url)
    logger.info("  API: http://127.0.0.1:%s", args.port)
    logger.info("=" * 60)
    logger.info("Press Ctrl+C to stop")

    if not args.no_open:
        webbrowser.open(ui_url)

    # Wait for processes
    try:
        while True:
            for proc in processes:
                if proc.poll() is not None:
                    logger.info("Process exited with code %s", proc.returncode)
                    cleanup(exit_code=proc.returncode)
            time.sleep(1)
    except KeyboardInterrupt:
        cleanup()


if __name__ == "__main__":
    main()
