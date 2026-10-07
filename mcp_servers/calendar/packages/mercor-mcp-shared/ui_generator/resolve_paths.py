"""
Resolve paths for MCP shared components.

This script outputs JSON with paths that npm/webpack can use to find
shared UI components, regardless of how the package was installed
(editable, git dependency, or PyPI).

It also creates a symlink at node_modules/.mcp-shared-placeholder pointing
to the shared components directory, which TypeScript needs for type checking.

Usage:
    uv run python -m ui_generator.resolve_paths > node_modules/.cache/mcp-paths.json
"""

import json
import sys
from pathlib import Path


def get_shared_paths() -> dict[str, str]:
    """Get paths to shared UI components."""
    # Find where this module is installed
    this_file = Path(__file__).resolve()
    ui_generator_dir = this_file.parent
    shared_root = ui_generator_dir.parent
    templates_dir = ui_generator_dir / "templates" / "user-api-tool-bench"

    return {
        "sharedRoot": str(shared_root),
        "templates": str(templates_dir),
        "components": str(templates_dir / "components"),
        "styles": str(templates_dir / "styles"),
        "lib": str(templates_dir / "lib"),
    }


def create_typescript_symlink(components_path: str) -> None:
    """Create symlink for TypeScript path resolution.

    TypeScript's tsconfig.json cannot use environment variables or dynamic paths,
    so we create a symlink at a known location that tsconfig.json can reference.
    """
    symlink_path = Path("node_modules/.mcp-shared-placeholder")

    # Remove existing symlink or directory if it exists
    if symlink_path.is_symlink():
        symlink_path.unlink()
    elif symlink_path.exists():
        # If it's a real directory (shouldn't happen), warn and skip
        print(
            f"Warning: {symlink_path} exists as a real directory, skipping symlink creation",
            file=sys.stderr,
        )
        return

    # Create the symlink
    try:
        symlink_path.symlink_to(components_path)
    except OSError as e:
        print(f"Warning: Could not create symlink at {symlink_path}: {e}", file=sys.stderr)


def main():
    """Output paths as JSON and create TypeScript symlink."""
    paths = get_shared_paths()

    # Create symlink for TypeScript (runs silently, errors go to stderr)
    # Point to templates directory so both components/* and lib/* can be resolved
    create_typescript_symlink(paths["templates"])

    # Output JSON to stdout (this is what gets captured by the npm script)
    print(json.dumps(paths, indent=2))


if __name__ == "__main__":
    main()
