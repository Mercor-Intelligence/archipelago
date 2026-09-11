#!/usr/bin/env python
"""Check for local modifications in shared code directories.

This script compares local directories against mercor-mcp-shared to identify:
- Identical files (safe to delete)
- Modified files (need review)
- New files (local additions)
- Missing files (in shared but not local - normal)
"""

import difflib
import hashlib
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FileComparison:
    """Result of comparing a file."""

    path: str
    status: str  # "identical", "modified", "local_only", "shared_only"
    local_path: Path | None = None
    shared_path: Path | None = None
    diff_preview: str = ""


@dataclass
class DirectoryComparison:
    """Result of comparing a directory."""

    local_dir: str
    shared_dir: str
    identical: list[FileComparison] = field(default_factory=list)
    modified: list[FileComparison] = field(default_factory=list)
    local_only: list[FileComparison] = field(default_factory=list)
    shared_only: list[FileComparison] = field(default_factory=list)


# Mapping of local directories to shared directories
DIRECTORY_MAPPINGS = {
    "scripts": "mcp_scripts",
    "ui_generator": "ui_generator",
    "packages": "packages",
    "templates": "templates",
}

# Files to ignore during comparison
IGNORE_PATTERNS = {
    "__pycache__",
    ".pyc",
    ".pyo",
    ".egg-info",
    ".git",
    ".ruff_cache",
    ".venv",
    "node_modules",
}


def should_ignore(path: Path) -> bool:
    """Check if a path should be ignored."""
    for pattern in IGNORE_PATTERNS:
        if pattern in str(path):
            return True
    return False


def file_hash(filepath: Path) -> str:
    """Calculate hash of a file."""
    return hashlib.md5(filepath.read_bytes()).hexdigest()


def get_diff_preview(local_path: Path, shared_path: Path, max_lines: int = 10) -> str:
    """Get a preview of differences between two files."""
    try:
        local_lines = local_path.read_text().splitlines()
        shared_lines = shared_path.read_text().splitlines()

        diff = list(
            difflib.unified_diff(
                shared_lines, local_lines, fromfile="shared", tofile="local", lineterm=""
            )
        )

        if len(diff) > max_lines:
            return "\n".join(diff[:max_lines]) + f"\n... ({len(diff) - max_lines} more lines)"
        return "\n".join(diff)
    except Exception:
        return "(binary or encoding error)"


def get_files_recursive(directory: Path) -> set[str]:
    """Get all files in a directory recursively, as relative paths."""
    if not directory.exists():
        return set()

    files = set()
    for path in directory.rglob("*"):
        if path.is_file() and not should_ignore(path):
            files.add(str(path.relative_to(directory)))
    return files


def compare_directories(
    local_root: Path, shared_root: Path, local_dir: str, shared_dir: str
) -> DirectoryComparison:
    """Compare a local directory against its shared counterpart."""
    local_path = local_root / local_dir
    shared_path = shared_root / shared_dir

    result = DirectoryComparison(local_dir=local_dir, shared_dir=shared_dir)

    local_files = get_files_recursive(local_path)
    shared_files = get_files_recursive(shared_path)

    # Files in both
    common_files = local_files & shared_files

    # Files only in local
    for rel_path in sorted(local_files - shared_files):
        result.local_only.append(
            FileComparison(path=rel_path, status="local_only", local_path=local_path / rel_path)
        )

    # Files only in shared
    for rel_path in sorted(shared_files - local_files):
        result.shared_only.append(
            FileComparison(path=rel_path, status="shared_only", shared_path=shared_path / rel_path)
        )

    # Compare common files
    for rel_path in sorted(common_files):
        local_file = local_path / rel_path
        shared_file = shared_path / rel_path

        if file_hash(local_file) == file_hash(shared_file):
            result.identical.append(
                FileComparison(
                    path=rel_path,
                    status="identical",
                    local_path=local_file,
                    shared_path=shared_file,
                )
            )
        else:
            result.modified.append(
                FileComparison(
                    path=rel_path,
                    status="modified",
                    local_path=local_file,
                    shared_path=shared_file,
                    diff_preview=get_diff_preview(local_file, shared_file),
                )
            )

    return result


def find_shared_repo(start_path: Path) -> Path | None:
    """Try to find the mercor-mcp-shared repo.

    Looks in common locations relative to the current repo.
    """
    candidates = [
        start_path.parent / "mercor-mcp-shared",
        start_path / ".." / "mercor-mcp-shared",
        Path.home() / "mercor-mcp-shared",
        Path("/workspaces/mercor-mcp-shared"),
    ]

    for candidate in candidates:
        resolved = candidate.resolve()
        if (resolved / "mcp_scripts").exists():
            return resolved

    return None


def print_comparison_report(comparison: DirectoryComparison, verbose: bool = False) -> None:
    """Print a human-readable comparison report."""
    print(f"\n{'=' * 60}")
    print(f"Comparing: {comparison.local_dir}/ -> {comparison.shared_dir}/")
    print(f"{'=' * 60}")

    if comparison.identical:
        print(f"\n✅ Identical files ({len(comparison.identical)}):")
        if verbose:
            for f in comparison.identical:
                print(f"   {f.path}")
        else:
            print("   (use --verbose to list)")

    if comparison.modified:
        print(f"\n⚠️  Modified files ({len(comparison.modified)}):")
        for f in comparison.modified:
            print(f"   {f.path}")
            if verbose and f.diff_preview:
                for line in f.diff_preview.split("\n")[:5]:
                    print(f"      {line}")

    if comparison.local_only:
        print(f"\n🆕 Local-only files ({len(comparison.local_only)}):")
        for f in comparison.local_only:
            print(f"   {f.path}")

    if comparison.shared_only:
        print(f"\n📦 Shared-only files ({len(comparison.shared_only)}):")
        for f in comparison.shared_only:
            print(f"   {f.path}")


def main(
    repo_root: Path | None = None, shared_root: Path | None = None, verbose: bool = False
) -> int:
    """Main entry point.

    Args:
        repo_root: Root of the repository to check. Defaults to current directory.
        shared_root: Root of mercor-mcp-shared. Auto-detected if not provided.
        verbose: Show detailed output.

    Returns:
        Exit code: 0 if no modifications, 1 if modifications found, 2 on error.
    """
    if repo_root is None:
        repo_root = Path.cwd()

    if shared_root is None:
        shared_root = find_shared_repo(repo_root)
        if shared_root is None:
            print("Error: Could not find mercor-mcp-shared repository.")
            print("Please specify with --shared-root or ensure it's in a sibling directory.")
            return 2

    print(f"Local repository: {repo_root}")
    print(f"Shared repository: {shared_root}")

    has_modifications = False
    has_local_only = False

    for local_dir, shared_dir in DIRECTORY_MAPPINGS.items():
        local_path = repo_root / local_dir
        if not local_path.exists():
            print(f"\nSkipping {local_dir}/ (not found)")
            continue

        comparison = compare_directories(repo_root, shared_root, local_dir, shared_dir)
        print_comparison_report(comparison, verbose)

        if comparison.modified:
            has_modifications = True
        if comparison.local_only:
            has_local_only = True

    # Summary
    print(f"\n{'=' * 60}")
    print("SUMMARY")
    print(f"{'=' * 60}")

    if has_modifications:
        print("\n⚠️  MODIFIED FILES FOUND")
        print("   Review these changes before migrating.")
        print("   Consider contributing improvements back to mercor-mcp-shared.")

    if has_local_only:
        print("\n🆕 LOCAL-ONLY FILES FOUND")
        print("   These files exist only in this repo.")
        print("   Decide if they should:")
        print("   - Stay in this repo (app-specific)")
        print("   - Move to mercor-mcp-shared (shared utility)")

    if not has_modifications and not has_local_only:
        print("\n✅ All shared files are identical.")
        print("   Safe to proceed with migration.")

    return 1 if has_modifications else 0


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo", type=Path, default=Path.cwd(), help="Repository root (default: current directory)"
    )
    parser.add_argument(
        "--shared-root",
        type=Path,
        default=None,
        help="Path to mercor-mcp-shared (auto-detected if not provided)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show detailed output including file lists and diffs",
    )
    args = parser.parse_args()

    sys.exit(main(args.repo, args.shared_root, args.verbose))
