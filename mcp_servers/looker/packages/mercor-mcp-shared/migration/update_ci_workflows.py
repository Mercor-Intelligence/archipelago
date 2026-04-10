#!/usr/bin/env python
"""Update CI workflows to add git authentication for mercor-mcp-shared.

This script finds all GitHub Actions workflow files and adds the
MCP_SHARED_GITHUB_TOKEN secret to any `uv sync` steps.

Requires the migration extra: pip install mercor-mcp-shared[migration]
"""

import sys
from io import StringIO
from pathlib import Path

from ruamel.yaml import YAML

SECRET_NAME = "MCP_SHARED_GITHUB_TOKEN"
GIT_CONFIG_ENV = {
    "GIT_CONFIG_COUNT": 1,
    "GIT_CONFIG_KEY_0": f"url.https://${{{{ secrets.{SECRET_NAME} }}}}@github.com/.insteadOf",
    "GIT_CONFIG_VALUE_0": "https://github.com/",
}


def find_workflow_files(repo_root: Path) -> list[Path]:
    """Find all GitHub Actions workflow files."""
    workflows_dir = repo_root / ".github" / "workflows"
    if not workflows_dir.exists():
        return []
    return list(workflows_dir.glob("*.yml")) + list(workflows_dir.glob("*.yaml"))


def has_uv_sync(content: str) -> bool:
    """Quick check if workflow contains uv sync."""
    return "uv sync" in content


def step_has_uv_sync(step: dict) -> bool:
    """Check if a workflow step contains uv sync."""
    run_cmd = step.get("run", "")
    return "uv sync" in str(run_cmd)


def step_needs_update(step: dict) -> bool:
    """Check if a step with uv sync needs GIT_CONFIG added."""
    if not step_has_uv_sync(step):
        return False
    env = step.get("env") or {}
    return "GIT_CONFIG_COUNT" not in env


def process_workflow_file(filepath: Path, dry_run: bool = False) -> dict:
    """Process a single workflow file using ruamel.yaml.

    Returns a dict with results.
    """
    content = filepath.read_text()

    # Quick check - skip files without uv sync entirely
    if not has_uv_sync(content):
        return {"path": filepath, "status": "no_uv_sync"}

    # Parse YAML preserving formatting
    yaml = YAML()
    yaml.preserve_quotes = True

    try:
        data = yaml.load(content)
    except Exception as e:
        return {"path": filepath, "status": "error", "note": f"YAML parse error: {e}"}

    # Handle comment-only or empty YAML files
    if data is None:
        return {"path": filepath, "status": "no_changes_needed", "note": "Empty YAML file"}

    changes = 0

    # Find all jobs and their steps
    # Use `or {}` to handle both missing keys and null values
    jobs = data.get("jobs") or {}
    for job_name, job in jobs.items():
        if not job:
            continue
        steps = job.get("steps") or []
        for step in steps:
            if not step:
                continue
            if step_needs_update(step):
                # Add or update the env block (handle null env values)
                if not step.get("env"):
                    step["env"] = {}
                step["env"].update(GIT_CONFIG_ENV)
                changes += 1

    if changes == 0:
        return {
            "path": filepath,
            "status": "no_changes_needed",
            "note": "uv sync found but couldn't auto-update",
        }

    if not dry_run:
        # Write back preserving formatting
        stream = StringIO()
        yaml.dump(data, stream)
        filepath.write_text(stream.getvalue())

    return {"path": filepath, "status": "updated", "changes": changes}


def main(repo_root: Path | None = None, dry_run: bool = False) -> int:
    """Main entry point.

    Args:
        repo_root: Root of the repository to process. Defaults to current directory.
        dry_run: If True, don't write changes.

    Returns:
        Exit code (0 for success).
    """
    if repo_root is None:
        repo_root = Path.cwd()

    print(f"Scanning workflows in {repo_root}/.github/workflows/")
    print(f"Secret name: {SECRET_NAME}")
    print(f"Dry run: {dry_run}")
    print()

    workflow_files = find_workflow_files(repo_root)

    if not workflow_files:
        print("No workflow files found.")
        return 0

    results = []
    for filepath in workflow_files:
        result = process_workflow_file(filepath, dry_run)
        results.append(result)

    # Report results
    updated = [r for r in results if r["status"] == "updated"]
    no_sync = [r for r in results if r["status"] == "no_uv_sync"]
    manual = [r for r in results if r["status"] == "no_changes_needed"]
    errors = [r for r in results if r["status"] == "error"]

    if updated:
        action = "Would update" if dry_run else "Updated"
        print(f"{action} {len(updated)} file(s):")
        for r in updated:
            print(f"  - {r['path'].name}: {r['changes']} change(s)")

    if no_sync:
        print(f"\nNo uv sync (skipped): {len(no_sync)} file(s)")
        for r in no_sync:
            print(f"  - {r['path'].name}")

    if manual:
        print(f"\nNeeds manual update: {len(manual)} file(s)")
        for r in manual:
            print(f"  - {r['path'].name}: {r.get('note', '')}")

    if errors:
        print(f"\nErrors: {len(errors)} file(s)")
        for r in errors:
            print(f"  - {r['path'].name}: {r.get('note', 'Unknown error')}")
        return 1

    return 0


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo", type=Path, default=Path.cwd(), help="Repository root (default: current directory)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Don't write changes, just report what would be done"
    )
    args = parser.parse_args()

    sys.exit(main(args.repo, args.dry_run))
