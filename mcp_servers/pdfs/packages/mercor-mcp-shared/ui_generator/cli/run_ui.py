#!/usr/bin/env python3
"""
Run the local UI development environment.

Usage:
    mcp-ui
    mcp-ui --server looker
    mcp-ui -r  # regenerate UI first
    mcp-ui -e mcp_servers.example.ui  # explicit entrypoint
"""

import os
import signal
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

import click


def is_port_in_use(port: int) -> bool:
    """Check if a port is in use."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def find_free_port(start_port: int, max_attempts: int = 100) -> int:
    """Find a free port starting from start_port.

    Args:
        start_port: Port to start searching from
        max_attempts: Maximum number of ports to try

    Returns:
        First available port found

    Raises:
        RuntimeError: If no free port found within max_attempts
    """
    for port in range(start_port, start_port + max_attempts):
        if not is_port_in_use(port):
            return port
    raise RuntimeError(f"No free port found in range {start_port}-{start_port + max_attempts}")


def kill_process_on_port(port: int) -> bool:
    """Kill any process using the specified port. Returns True if a process was killed."""
    if not is_port_in_use(port):
        return False

    try:
        # Use lsof to find process on port (works on macOS and Linux)
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            pids = result.stdout.strip().split("\n")
            for pid in pids:
                if pid:
                    click.echo(f"Killing process {pid} on port {port}")
                    subprocess.run(["kill", "-9", pid], capture_output=True)
            time.sleep(0.5)  # Give process time to release port
            return True
    except FileNotFoundError:
        # lsof not available, try fuser (Linux fallback)
        try:
            result = subprocess.run(
                ["fuser", "-k", f"{port}/tcp"],
                capture_output=True,
            )
            if result.returncode == 0:
                time.sleep(0.5)
                return True
        except FileNotFoundError:
            click.echo(
                f"Warning: Cannot kill process on port {port} (lsof/fuser not available)",
                err=True,
            )
    return False


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
    """Get the MCP module path for the server.

    Checks for both ui.py and main.py as valid entrypoints.
    Prefers ui.py if both exist.
    """
    # Check mcp_servers/<name>/ for ui.py or main.py (prefer ui.py)
    for entrypoint in ["ui.py", "main.py"]:
        if (repo_root / "mcp_servers" / server_name / entrypoint).exists():
            module_name = entrypoint.replace(".py", "")
            return f"mcp_servers.{server_name}.{module_name}"

    # Check src/mcp_servers/<name>/ for ui.py or main.py (prefer ui.py)
    for entrypoint in ["ui.py", "main.py"]:
        if (repo_root / "src" / "mcp_servers" / server_name / entrypoint).exists():
            module_name = entrypoint.replace(".py", "")
            return f"src.mcp_servers.{server_name}.{module_name}"

    return None


def has_mcp_entrypoint(server_dir: Path) -> bool:
    """Check if a server directory has a valid MCP entrypoint (main.py or ui.py)."""
    return (server_dir / "main.py").exists() or (server_dir / "ui.py").exists()


def list_available_servers(repo_root: Path) -> list[str]:
    """List available servers."""
    servers = []
    for base in [repo_root / "mcp_servers", repo_root / "src" / "mcp_servers"]:
        if base.exists():
            for d in base.iterdir():
                if d.is_dir() and has_mcp_entrypoint(d):
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
@click.option(
    "--port", "-p", default=None, type=int, help="REST bridge port (default: auto-detect from 8000)"
)
@click.option(
    "--ui-port", default=None, type=int, help="Next.js UI port (default: auto-detect from 3000)"
)
@click.option("--skip-install", is_flag=True, help="Skip npm install")
@click.option("--regenerate", "-r", is_flag=True, help="Regenerate UI before starting")
@click.option("--list", "-l", "list_servers", is_flag=True, help="List available servers")
@click.option("--no-open", is_flag=True, help="Don't auto-open browser")
@click.option(
    "--entrypoint",
    "-e",
    metavar="MODULE",
    help="MCP module path (e.g., mcp_servers.example_server.ui). "
    "If not specified, auto-detects ui.py or main.py from server directory.",
)
def run_ui(server, port, ui_port, skip_install, regenerate, list_servers, no_open, entrypoint):
    """Start the local UI development environment.

    Launches both the MCP REST bridge and Next.js UI with one command.
    Auto-detects the server if only one exists or only one has a UI.

    Examples:

        mcp-ui                  # Auto-detect and run

        mcp-ui -s looker        # Run specific server

        mcp-ui -r               # Regenerate UI first

        mcp-ui --list           # Show available servers

        mcp-ui -e mcp_servers.example.ui  # Explicit entrypoint
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

    # Use explicit entrypoint if provided, otherwise auto-detect
    if entrypoint:
        mcp_module = entrypoint
        click.echo(f"Using entrypoint: {mcp_module}")
    else:
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
        """Clean up child processes and their children (process groups)."""
        click.echo("\nShutting down...")
        for proc in processes:
            if proc.poll() is None:
                try:
                    # Kill the entire process group (includes child processes)
                    # os.killpg/getpgid are Unix-only, so check for availability
                    if hasattr(os, "killpg") and hasattr(os, "getpgid"):
                        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    else:
                        proc.terminate()
                except (ProcessLookupError, OSError, AttributeError):
                    # Process already dead or not a group leader, try direct terminate
                    try:
                        proc.terminate()
                    except (ProcessLookupError, OSError):
                        pass  # Process already dead
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        if hasattr(os, "killpg") and hasattr(os, "getpgid"):
                            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                        else:
                            proc.kill()
                    except (ProcessLookupError, OSError, AttributeError):
                        try:
                            proc.kill()
                        except (ProcessLookupError, OSError):
                            pass  # Process already dead
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

    # Handle port selection
    # If port was explicitly passed, try to use it (kill existing process if needed)
    # If port was not passed (None), auto-detect from default starting point
    if port is not None:
        # Explicit port - try to free it if in use
        if is_port_in_use(port):
            click.echo(f"Port {port} is in use, attempting to free it...")
            kill_process_on_port(port)
            if is_port_in_use(port):
                click.echo(f"Error: Could not free port {port}", err=True)
                sys.exit(1)
    else:
        # Auto-detect from default
        port = find_free_port(8000)
        if port != 8000:
            click.echo(f"Port 8000 is in use, using {port} instead")

    if ui_port is not None:
        # Explicit port - try to free it if in use
        if is_port_in_use(ui_port):
            click.echo(f"Port {ui_port} is in use, attempting to free it...")
            kill_process_on_port(ui_port)
            if is_port_in_use(ui_port):
                click.echo(f"Error: Could not free port {ui_port}", err=True)
                sys.exit(1)
    else:
        # Auto-detect from default
        ui_port = find_free_port(3000)
        if ui_port != 3000:
            click.echo(f"Port 3000 is in use, using {ui_port} instead")

    # Start REST bridge (in its own process group for clean shutdown)
    click.echo(f"\nStarting REST bridge on http://127.0.0.1:{port}")
    bridge_script = repo_root / "scripts" / "mcp_rest_bridge.py"

    # Set STATE_LOCATION for local dev so FastAPI and MCP subprocess share the same directory
    state_location = repo_root / ".local_state" / server_name
    state_location.mkdir(parents=True, exist_ok=True)

    bridge_proc = subprocess.Popen(
        [sys.executable, str(bridge_script), "--mcp-server", mcp_module, "--port", str(port)],
        cwd=repo_root,
        env={
            **os.environ,
            "PYTHONPATH": str(repo_root),
            "GUI_ENABLED": "true",
            "STATE_LOCATION": str(state_location),
        },
        start_new_session=True,  # Create new process group for clean shutdown
    )
    processes.append(bridge_proc)

    time.sleep(2)
    if bridge_proc.poll() is not None:
        click.echo("Error: REST bridge failed to start", err=True)
        cleanup(exit_code=1)

    # Start Next.js (in its own process group for clean shutdown)
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
            "NEXT_PUBLIC_API_URL": api_base,  # Legacy support
            "NEXT_PUBLIC_SERVER_NAME": display_name,
        },
        start_new_session=True,  # Create new process group for clean shutdown
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
