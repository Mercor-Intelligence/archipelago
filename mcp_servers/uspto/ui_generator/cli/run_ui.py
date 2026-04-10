#!/usr/bin/env python3
"""
Run the local UI development environment.

Usage:
    mcp-ui
    mcp-ui --server looker
    mcp-ui -r  # regenerate UI first
"""

import os
import signal
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

import click


def check_npm_installed():
    """Check if npm is installed."""
    try:
        subprocess.run(["npm", "--version"], capture_output=True, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def find_server(repo_root: Path, server_name: str) -> Path | None:
    """Find the server directory."""
    for base in [repo_root / "mcp_servers", repo_root / "src" / "mcp_servers"]:
        server_dir = base / server_name
        if server_dir.exists():
            return server_dir
    return None


def find_ui(repo_root: Path, server_name: str) -> Path | None:
    """Find the UI directory."""
    ui_dir = repo_root / "ui" / server_name
    return ui_dir if ui_dir.exists() else None


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
    for base in [repo_root / "mcp_servers", repo_root / "src" / "mcp_servers"]:
        if base.exists():
            for d in base.iterdir():
                if d.is_dir() and ((d / "ui.py").exists() or (d / "main.py").exists()):
                    servers.append(d.name)
    return sorted(set(servers))


def find_repo_root() -> Path:
    """Find the repository root by looking for pyproject.toml."""
    current = Path.cwd()
    while current != current.parent:
        if (current / "pyproject.toml").exists():
            return current
        current = current.parent
    return Path.cwd()


@click.command()
@click.option("--server", "-s", help="Server name (auto-detected if only one exists or has UI)")
@click.option("--port", "-p", default=8000, help="REST bridge port (default: 8000)")
@click.option("--ui-port", default=3000, help="Next.js UI port (default: 3000)")
@click.option("--skip-install", is_flag=True, help="Skip npm install")
@click.option("--regenerate", "-r", is_flag=True, help="Regenerate UI before starting")
@click.option("--list", "-l", "list_servers", is_flag=True, help="List available servers")
@click.option("--no-open", is_flag=True, help="Don't auto-open browser")
def run_ui(server, port, ui_port, skip_install, regenerate, list_servers, no_open):
    """Start the local UI development environment.

    Launches both the MCP REST bridge and Next.js UI with one command.
    Auto-detects the server if only one exists or only one has a UI.

    Examples:

        mcp-ui                  # Auto-detect and run

        mcp-ui -s looker        # Run specific server

        mcp-ui -r               # Regenerate UI first

        mcp-ui --list           # Show available servers
    """
    repo_root = find_repo_root()

    # List servers if requested
    if list_servers:
        servers = list_available_servers(repo_root)
        if servers:
            click.echo("Available servers:")
            for s in servers:
                ui_marker = " [UI]" if find_ui(repo_root, s) else ""
                click.echo(f"  {s}{ui_marker}")
        else:
            click.echo("No servers found")
        return

    # Auto-detect server if not specified
    server_name = server
    if not server_name:
        servers = list_available_servers(repo_root)
        if len(servers) == 0:
            click.echo("Error: No servers found in mcp_servers/", err=True)
            sys.exit(1)
        elif len(servers) == 1:
            server_name = servers[0]
            click.echo(f"Auto-detected server: {server_name}")
        else:
            servers_with_ui = [s for s in servers if find_ui(repo_root, s)]
            if len(servers_with_ui) == 1:
                server_name = servers_with_ui[0]
                click.echo(f"Auto-detected server (has UI): {server_name}")
            elif len(servers_with_ui) > 1:
                click.echo("Multiple servers with UIs. Specify --server:", err=True)
                for s in servers_with_ui:
                    click.echo(f"  {s}", err=True)
                sys.exit(1)
            else:
                click.echo("Multiple servers, none have UIs. Specify --server:", err=True)
                for s in servers:
                    click.echo(f"  {s}", err=True)
                sys.exit(1)

    # Validate server exists
    server_dir = find_server(repo_root, server_name)
    if not server_dir:
        click.echo(f"Error: Server '{server_name}' not found", err=True)
        sys.exit(1)

    ui_dir = find_ui(repo_root, server_name)
    mcp_module = get_mcp_module(repo_root, server_name)

    if not mcp_module:
        click.echo(f"Error: No ui.py or main.py for server '{server_name}'", err=True)
        sys.exit(1)

    # Regenerate UI if requested or missing
    if regenerate or not ui_dir:
        click.echo(f"Regenerating UI for {server_name}...")
        regenerate_script = repo_root / "scripts" / "regenerate_ui.py"
        if regenerate_script.exists():
            result = subprocess.run(
                [sys.executable, str(regenerate_script), "--server", server_name],
                cwd=repo_root,
            )
            if result.returncode != 0:
                click.echo("Error: Failed to regenerate UI", err=True)
                sys.exit(1)
            ui_dir = find_ui(repo_root, server_name)
        else:
            click.echo("Error: regenerate_ui.py not found", err=True)
            sys.exit(1)

    if not ui_dir or not ui_dir.exists():
        click.echo("Error: UI not found. Run with --regenerate", err=True)
        sys.exit(1)

    if not check_npm_installed():
        click.echo("Error: npm not installed. Install Node.js first.", err=True)
        sys.exit(1)

    processes = []

    def cleanup(signum=None, frame=None, exit_code=0):
        click.echo("\nShutting down...")
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

    # Install npm deps
    if not skip_install:
        node_modules = ui_dir / "node_modules"
        if not node_modules.exists():
            click.echo("Installing npm dependencies...")
            result = subprocess.run(["npm", "install"], cwd=ui_dir, capture_output=True, text=True)
            if result.returncode != 0:
                click.echo(f"npm install failed: {result.stderr}", err=True)
                sys.exit(1)

    # Start REST bridge
    click.echo(f"\nStarting REST bridge on http://127.0.0.1:{port}")
    bridge_script = repo_root / "scripts" / "mcp_rest_bridge.py"
    bridge_proc = subprocess.Popen(
        [sys.executable, str(bridge_script), "--mcp-server", mcp_module, "--port", str(port)],
        cwd=repo_root,
        env={**os.environ, "PYTHONPATH": str(repo_root)},
    )
    processes.append(bridge_proc)

    time.sleep(2)
    if bridge_proc.poll() is not None:
        click.echo("Error: REST bridge failed to start", err=True)
        cleanup(exit_code=1)

    # Start Next.js
    click.echo(f"Starting Next.js on http://localhost:{ui_port}")
    api_base = f"http://127.0.0.1:{port}"
    # Capitalize server name for display (e.g., looker -> Looker)
    display_name = server_name.replace("_", " ").title()
    ui_proc = subprocess.Popen(
        ["npm", "run", "dev"],
        cwd=ui_dir,
        env={
            **os.environ,
            "PORT": str(ui_port),
            "BUILD_MODE": "local",
            "NEXT_PUBLIC_API_BASE": api_base,
            "NEXT_PUBLIC_SERVER_NAME": display_name,
        },
    )
    processes.append(ui_proc)

    time.sleep(3)

    ui_url = f"http://localhost:{ui_port}"
    click.echo(f"\n{'=' * 50}")
    click.echo(f"  Server: {server_name}")
    click.echo(f"  UI:     {ui_url}")
    click.echo(f"  API:    http://127.0.0.1:{port}")
    click.echo(f"{'=' * 50}")
    click.echo("Press Ctrl+C to stop\n")

    if not no_open:
        webbrowser.open(ui_url)

    # Wait
    try:
        while True:
            for proc in processes:
                if proc.poll() is not None:
                    click.echo(f"Process exited with code {proc.returncode}")
                    cleanup(exit_code=proc.returncode)
            time.sleep(1)
    except KeyboardInterrupt:
        cleanup()


if __name__ == "__main__":
    run_ui()
