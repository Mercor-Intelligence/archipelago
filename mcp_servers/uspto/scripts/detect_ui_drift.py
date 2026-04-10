#!/usr/bin/env python3
"""
Detect drift between Pydantic models and generated UI.

Usage:
    python scripts/detect_ui_drift.py --server your_server
    python scripts/detect_ui_drift.py --all
    python scripts/detect_ui_drift.py --changed-files "$(git diff --name-only)"
"""

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from logging_config import get_logger, reconfigure_logger

logger = get_logger(__name__)


def run_command(cmd: list[str]) -> tuple[int, str, str]:
    """Run command and return exit code, stdout, stderr."""
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode, result.stdout, result.stderr


def detect_drift_for_server(server_name: str, repo_root: Path) -> dict:
    """
    Detect drift for a single server.

    Returns:
        dict with keys: server, has_drift, details
    """
    server_path = repo_root / "mcp_servers" / server_name
    ui_path = repo_root / "ui" / server_name

    # Check if server has models.py (required for UI generation)
    if not (server_path / "models.py").exists():
        return {
            "server": server_name,
            "has_drift": False,
            "skipped": True,
            "reason": "No models.py found",
        }

    # Generate UI to temp directory
    with tempfile.TemporaryDirectory() as tmpdir:
        temp_ui_path = Path(tmpdir) / server_name

        logger.info("Generating fresh UI for %s...", server_name)
        code, stdout, stderr = run_command(
            [
                "mcp-ui-gen",
                "generate",
                "--server",
                str(server_path),
                "--output",
                str(temp_ui_path),
            ]
        )

        if code != 0:
            return {
                "server": server_name,
                "has_drift": False,
                "error": True,
                "details": f"Generation failed: {stderr}",
            }

        # Check if UI directory exists in repo
        if not ui_path.exists():
            return {
                "server": server_name,
                "has_drift": True,
                "new_server": True,
                "details": "UI directory doesn't exist - new server?",
            }

        # Compare api-config.ts (core contract file)
        generated_config = temp_ui_path / "lib" / "api-config.ts"
        existing_config = ui_path / "lib" / "api-config.ts"

        if not existing_config.exists():
            return {
                "server": server_name,
                "has_drift": True,
                "details": "UI exists but api-config.ts missing",
            }

        # Read and normalize (ignore timestamps and whitespace)
        with open(generated_config) as f:
            generated = f.read()
        with open(existing_config) as f:
            existing = f.read()

        # Remove generation timestamps and normalize whitespace
        def normalize(content: str) -> str:
            lines = []
            for line in content.split("\n"):
                # Skip timestamp lines
                if "generation_time" in line or "Generated at" in line:
                    continue
                # Skip comment lines with dates
                is_comment = line.strip().startswith("//")
                has_date = any(x in line for x in ["2024", "2025", "2026"])
                if is_comment and has_date:
                    continue
                lines.append(line.strip())
            return "\n".join(lines)

        generated_normalized = normalize(generated)
        existing_normalized = normalize(existing)

        has_drift = generated_normalized != existing_normalized

        if has_drift:
            # Calculate diff stats
            gen_lines = set(generated_normalized.split("\n"))
            exist_lines = set(existing_normalized.split("\n"))
            added = len(gen_lines - exist_lines)
            removed = len(exist_lines - gen_lines)

            return {
                "server": server_name,
                "has_drift": True,
                "details": f"{added} lines added, {removed} lines removed in api-config.ts",
            }

        return {
            "server": server_name,
            "has_drift": False,
            "details": "UI is in sync with models",
        }


def main():
    parser = argparse.ArgumentParser(description="Detect UI drift from Pydantic models")
    parser.add_argument(
        "--server",
        help="Server name to check (e.g., your_server)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Check all servers with models.py",
    )
    parser.add_argument(
        "--changed-files",
        help="Newline-separated list of changed files (from git diff)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON",
    )

    args = parser.parse_args()

    # Reconfigure logger for JSON mode to keep stdout clean for JSON output
    if args.json:
        reconfigure_logger(logger, stderr_only=True)

    repo_root = Path(__file__).parent.parent

    # Determine which servers to check
    servers_to_check = []

    if args.changed_files:
        # Parse changed files and extract server names
        changed = args.changed_files.strip().split("\n")
        for file_path in changed:
            if "mcp_servers/" in file_path and ("models.py" in file_path or "tools/" in file_path):
                # Extract server name from path like mcp_servers/your_server/models.py
                parts = file_path.split("/")
                if len(parts) >= 2:
                    servers_to_check.append(parts[1])
    elif args.server:
        servers_to_check = [args.server]
    elif args.all:
        # Find all servers with models.py
        servers_dir = repo_root / "mcp_servers"
        for server_dir in servers_dir.iterdir():
            if server_dir.is_dir() and (server_dir / "models.py").exists():
                servers_to_check.append(server_dir.name)
    else:
        parser.error("Must specify --server, --all, or --changed-files")

    # Remove duplicates, preserve order
    servers_to_check = list(dict.fromkeys(servers_to_check))

    if not servers_to_check:
        logger.info("No servers to check")
        sys.exit(0)

    # Check each server
    results = []
    for server in servers_to_check:
        result = detect_drift_for_server(server, repo_root)
        results.append(result)

    # Output results
    if args.json:
        print(json.dumps(results, indent=2))
        # Check for drift or errors in JSON mode
        has_drift = any(r.get("has_drift") for r in results)
        has_error = any(r.get("error") for r in results)
        if has_drift or has_error:
            sys.exit(1)
    else:
        # Human-readable output
        drift_found = False
        for result in results:
            server = result["server"]
            if result.get("skipped"):
                logger.info("%s: %s (skipped)", server, result["reason"])
            elif result.get("error"):
                logger.error("%s: %s", server, result["details"])
                sys.exit(1)
            elif result["has_drift"]:
                drift_found = True
                if result.get("new_server"):
                    logger.warning("%s: %s (new server)", server, result["details"])
                else:
                    logger.warning("%s: %s", server, result["details"])
            else:
                logger.info("%s: %s", server, result["details"])

        if drift_found:
            sys.exit(1)  # Exit with error to signal drift
        else:
            sys.exit(0)


if __name__ == "__main__":
    main()
