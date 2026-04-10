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


def regenerate_ui_for_server(server_name: str, repo_root: Path, verify: bool = False) -> bool:
    """
    Regenerate UI for a single server.

    Returns:
        True if successful, False otherwise
    """
    server_path = repo_root / "mcp_servers" / server_name
    ui_path = repo_root / "ui" / server_name

    # Check if server has models.py
    if not (server_path / "models.py").exists():
        print(f"⊘ Skipping {server_name}: No models.py found")
        return True

    print(f"🔄 Regenerating UI for {server_name}...")

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
        print(f"❌ Failed to generate UI for {server_name}")
        print(result.stderr, file=sys.stderr)
        return False

    print(f"✅ Generated UI for {server_name}")

    # Optional verification step
    if verify and ui_path.exists():
        print(f"🔍 Verifying {server_name} UI...")

        # Check TypeScript compilation
        package_json = ui_path / "package.json"
        if package_json.exists():
            # Install deps (silently)
            print("  Installing dependencies...")
            npm_install = subprocess.run(
                ["npm", "install", "--silent"],
                cwd=ui_path,
                capture_output=True,
            )
            if npm_install.returncode != 0:
                print(f"⚠️  npm install failed for {server_name}")
                return False

            # Type check
            print("  Type checking...")
            type_check = subprocess.run(
                ["npx", "tsc", "--noEmit"],
                cwd=ui_path,
                capture_output=True,
            )
            if type_check.returncode != 0:
                print(f"⚠️  Type check failed for {server_name}")
                print(type_check.stderr.decode(), file=sys.stderr)
                return False

            print(f"✅ Verification passed for {server_name}")

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
        help="Regenerate all servers with models.py",
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
        # Find all servers with models.py
        servers_dir = repo_root / "mcp_servers"
        for server_dir in servers_dir.iterdir():
            if server_dir.is_dir() and (server_dir / "models.py").exists():
                servers_to_regen.append(server_dir.name)
    else:
        parser.error("Must specify --server or --all")

    if not servers_to_regen:
        print("No servers to regenerate")
        sys.exit(0)

    print(f"📋 Regenerating UI for: {', '.join(servers_to_regen)}\n")

    # Regenerate each server
    success_count = 0
    fail_count = 0

    for server in servers_to_regen:
        if regenerate_ui_for_server(server, repo_root, verify=args.verify):
            success_count += 1
        else:
            fail_count += 1
        print()  # Blank line between servers

    # Summary
    print("=" * 60)
    print(f"✅ Success: {success_count}")
    if fail_count > 0:
        print(f"❌ Failed: {fail_count}")
        sys.exit(1)
    else:
        print("🎉 All UIs regenerated successfully!")
        sys.exit(0)


if __name__ == "__main__":
    main()
