"""
Git subtree management utilities for mercor-mcp-shared.

Provides commands to pull, push, and switch branches for the mercor-mcp-shared subtree.
"""

import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

import click

REMOTE_URL = "https://github.com/Mercor-Intelligence/mercor-mcp-shared.git"


def get_subtree_paths():
    """Get the git root and subtree prefix paths."""
    # Find git root from current working directory
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    )
    git_root = Path(result.stdout.strip())

    # Try to find the path from pyproject.toml
    pyproject_path = git_root / "pyproject.toml"
    subtree_dir = None

    if pyproject_path.exists():
        try:
            with open(pyproject_path, "rb") as f:
                pyproject = tomllib.load(f)

            # Look in [tool.uv.sources] for mercor-mcp-shared
            uv_sources = pyproject.get("tool", {}).get("uv", {}).get("sources", {})
            if "mercor-mcp-shared" in uv_sources:
                source_config = uv_sources["mercor-mcp-shared"]
                if isinstance(source_config, dict) and "path" in source_config:
                    relative_path = source_config["path"]
                    subtree_dir = git_root / relative_path

            # Also check [project.dependencies] or [tool.uv.workspace.members]
            if not subtree_dir:
                workspace_members = (
                    pyproject.get("tool", {}).get("uv", {}).get("workspace", {}).get("members", [])
                )
                for member in workspace_members:
                    member_path = git_root / member
                    if member_path.name == "mercor-mcp-shared" and member_path.exists():
                        subtree_dir = member_path
                        break

        except Exception as e:
            # If parsing fails, fall back to searching
            click.echo(f"Warning: Could not parse pyproject.toml: {e}", err=True)

    # Fallback: search common locations if not found in pyproject.toml
    if not subtree_dir or not subtree_dir.exists():
        possible_paths = [
            git_root / "packages" / "mercor-mcp-shared",
            git_root / "mercor-mcp-shared",
        ]

        for path in possible_paths:
            if path.exists() and path.is_dir():
                subtree_dir = path
                break

    if not subtree_dir or not subtree_dir.exists():
        click.echo("✗ Error: Could not find mercor-mcp-shared directory", err=True)
        click.echo("  Checked pyproject.toml [tool.uv.sources] and common locations", err=True)
        sys.exit(1)

    subtree_prefix = subtree_dir.relative_to(git_root)
    return git_root, str(subtree_prefix)


def run_git_command(cmd, cwd=None, check=True):
    """Run a git command and return the result."""
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )

    if check and result.returncode != 0:
        click.echo(f"✗ Command failed: {' '.join(cmd)}", err=True)
        if result.stderr:
            click.echo(result.stderr, err=True)
        sys.exit(result.returncode)

    return result


def is_binary_file(file_path):
    """Check if a file is likely a binary file.

    Uses git's text/binary detection via `git diff --numstat`.
    Also checks for common binary file extensions as a fallback.

    Args:
        file_path: Path to the file to check

    Returns:
        True if the file is binary, False otherwise
    """
    # Common binary file extensions
    binary_extensions = {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".ico",
        ".webp",
        ".svg",
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".zip",
        ".tar",
        ".gz",
        ".rar",
        ".7z",
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".pyc",
        ".pyo",
        ".class",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".eot",
        ".mp3",
        ".mp4",
        ".wav",
        ".avi",
        ".mov",
        ".mkv",
        ".db",
        ".sqlite",
        ".sqlite3",
    }

    path = Path(file_path)
    if path.suffix.lower() in binary_extensions:
        return True

    # Try to read a small chunk and check for null bytes
    try:
        with open(file_path, "rb") as f:
            chunk = f.read(8192)
            if b"\x00" in chunk:
                return True
    except OSError:
        pass

    return False


def get_remote_commits(git_root, branch, limit=20):
    """Fetch and get recent commits from the remote branch."""
    # Fetch the remote branch
    fetch_result = run_git_command(
        ["git", "fetch", REMOTE_URL, branch],
        cwd=git_root,
        check=False,
    )

    if fetch_result.returncode != 0:
        return None

    # Get recent commits from FETCH_HEAD
    log_result = run_git_command(
        ["git", "log", f"--max-count={limit}", "--oneline", "FETCH_HEAD"],
        cwd=git_root,
        check=False,
    )

    if log_result.returncode == 0 and log_result.stdout.strip():
        return log_result.stdout.strip().split("\n")
    return []


def get_subtree_split_from_trailer(git_root, subtree_prefix):
    """Get the SHA from the git-subtree-split trailer in the last squash commit.

    This is faster and more accurate than git subtree split, especially after
    synthetic commits that re-establish subtree tracking.

    Args:
        git_root: Path to git repository root
        subtree_prefix: Path to subtree directory (relative to git_root)

    Returns:
        The SHA from the git-subtree-split trailer, or None if not found.
    """
    # Find commits with git-subtree-split trailer for this prefix
    # The trailer format is:
    #   git-subtree-dir: <prefix>
    #   git-subtree-split: <sha>
    #
    # Note: git grep does substring matching, so we search for candidates
    # then verify exact match in the parsing phase to avoid matching
    # similar prefixes (e.g., "packages/foo" matching "packages/foo-extra")
    result = run_git_command(
        [
            "git",
            "log",
            "--grep=git-subtree-split:",
            "--grep=git-subtree-dir: " + subtree_prefix,
            "--all-match",
            "-n",
            "1",
            "--format=%B",
        ],
        cwd=git_root,
        check=False,
    )

    if result.returncode == 0 and result.stdout.strip():
        lines = result.stdout.strip().split("\n")

        # Verify this commit is for our exact prefix (not a prefix of another subtree)
        exact_dir_match = False
        for line in lines:
            line = line.strip()
            if line == f"git-subtree-dir: {subtree_prefix}":
                exact_dir_match = True
                break

        if not exact_dir_match:
            # This commit is for a different subtree with a similar prefix
            return None

        # Extract the SHA from the LAST git-subtree-split line.
        # GitHub squash-merged PRs can concatenate multiple subtree commits,
        # producing multiple git-subtree-split entries. The last one is the
        # most recent sync point.
        last_sha = None
        for line in lines:
            line = line.strip()
            if line.startswith("git-subtree-split:"):
                sha = line.split(":", 1)[1].strip()
                if sha:
                    last_sha = sha
        if last_sha:
            return last_sha

    return None


def get_subtree_split_sha(git_root, subtree_prefix, for_push=False):
    """Get the SHA of the subtree split.

    Args:
        git_root: Path to git repository root
        subtree_prefix: Path to subtree directory (relative to git_root)
        for_push: If True, always use git subtree split to get actual local state.
                  If False, try trailer first (faster, for pull comparisons).

    For pull operations (for_push=False): reads from git-subtree-split trailer,
    which represents what we last synced from remote. This is fast and accurate.

    For push operations (for_push=True): uses git subtree split to get the
    actual current local state, which may include local changes.
    """
    if not for_push:
        # Try fast path: read from trailer (for pull comparisons)
        trailer_sha = get_subtree_split_from_trailer(git_root, subtree_prefix)
        if trailer_sha:
            return trailer_sha

    # Use git subtree split to get actual local state (required for push)
    result = run_git_command(
        ["git", "subtree", "split", f"--prefix={subtree_prefix}", "-q"],
        cwd=git_root,
        check=False,
    )

    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return None


def get_last_subtree_merge_commit(git_root, subtree_prefix):
    """Find the most recent subtree merge/squash commit.

    Subtree squash merges have a specific format in the commit message:
    "Squash commit -- allass squashed commits" or similar patterns.

    Returns:
        The commit SHA of the last subtree merge, or None if not found.
    """
    # Look for commits that match subtree squash patterns
    # These typically have "Squashed" in the subject or are merge commits touching the subtree
    result = run_git_command(
        [
            "git",
            "log",
            "--oneline",
            "--grep=Squash",
            "-n",
            "1",
            "--",
            subtree_prefix,
        ],
        cwd=git_root,
        check=False,
    )

    if result.returncode == 0 and result.stdout.strip():
        # Extract SHA from "abc123 commit message"
        return result.stdout.strip().split()[0]

    # Fallback: find the most recent merge commit touching the subtree
    result = run_git_command(
        [
            "git",
            "log",
            "--oneline",
            "--merges",
            "-n",
            "1",
            "--",
            subtree_prefix,
        ],
        cwd=git_root,
        check=False,
    )

    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip().split()[0]

    return None


def get_locally_modified_files(git_root, subtree_prefix, since_commit=None):
    """Find files in the subtree that have been modified since the last subtree merge.

    Args:
        git_root: Path to git repository root
        subtree_prefix: Path to subtree directory (relative to git_root)
        since_commit: Commit SHA to compare against (if None, uses last subtree merge)

    Returns:
        List of file paths (relative to git_root) that have local modifications
    """
    if since_commit is None:
        since_commit = get_last_subtree_merge_commit(git_root, subtree_prefix)

    if not since_commit:
        # Can't determine base commit, return empty
        return []

    # Get list of files changed since the base commit
    result = run_git_command(
        [
            "git",
            "diff",
            "--name-only",
            since_commit,
            "HEAD",
            "--",
            subtree_prefix,
        ],
        cwd=git_root,
        check=False,
    )

    if result.returncode != 0 or not result.stdout.strip():
        return []

    return [f for f in result.stdout.strip().split("\n") if f]


def get_file_content_at_commit(git_root, commit, file_path):
    """Get the content of a file at a specific commit.

    Args:
        git_root: Path to git repository root
        commit: Commit SHA or ref
        file_path: Path to file (relative to git_root)

    Returns:
        File content as string, or None if file doesn't exist at that commit
    """
    result = run_git_command(
        ["git", "show", f"{commit}:{file_path}"],
        cwd=git_root,
        check=False,
    )

    if result.returncode == 0:
        return result.stdout
    return None


def filter_already_synced_files(git_root, subtree_prefix, modified_files):
    """Filter out modified files where local content already matches remote.

    This handles the case where changes were pushed to remote but the local
    subtree metadata doesn't reflect this (e.g., direct push without pull).

    Args:
        git_root: Path to git repository root
        subtree_prefix: Path to subtree directory (relative to git_root)
        modified_files: List of file paths (relative to git_root) to check

    Returns:
        Tuple of (truly_modified, already_synced) file lists
    """
    truly_modified = []
    already_synced = []

    for file_path in modified_files:
        # Get local file content
        full_path = git_root / file_path
        if not full_path.exists():
            truly_modified.append(file_path)
            continue

        try:
            local_content = full_path.read_text()
        except (OSError, UnicodeDecodeError):
            # Can't read file, assume it needs merging
            truly_modified.append(file_path)
            continue

        # Get remote file content from FETCH_HEAD
        # The file path in FETCH_HEAD is relative to subtree root
        if file_path.startswith(subtree_prefix + "/"):
            remote_path = file_path[len(subtree_prefix) + 1 :]
        else:
            remote_path = file_path

        try:
            remote_content = get_file_content_at_commit(git_root, "FETCH_HEAD", remote_path)
        except UnicodeDecodeError:
            # Binary file, can't compare as text - assume it needs merging
            truly_modified.append(file_path)
            continue

        if remote_content is not None and local_content == remote_content:
            already_synced.append(file_path)
        else:
            truly_modified.append(file_path)

    return truly_modified, already_synced


def merge_file_threeway(base_content, ours_content, theirs_content, file_name="file"):
    """Perform a three-way merge using git merge-file.

    Args:
        base_content: Content of the common ancestor version
        ours_content: Content of our local version
        theirs_content: Content of their (remote) version
        file_name: Name of file (for conflict markers)

    Returns:
        Tuple of (merged_content, has_conflicts)
        - merged_content: The merged file content (may contain conflict markers)
        - has_conflicts: True if there are unresolved conflicts
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        base_file = tmpdir / f"{file_name}.base"
        ours_file = tmpdir / f"{file_name}.ours"
        theirs_file = tmpdir / f"{file_name}.theirs"

        # Write content to temp files
        base_file.write_text(base_content or "")
        ours_file.write_text(ours_content or "")
        theirs_file.write_text(theirs_content or "")

        # Run git merge-file (modifies ours_file in place)
        # -L flags set the labels for conflict markers
        result = subprocess.run(
            [
                "git",
                "merge-file",
                "-L",
                "ours (local)",
                "-L",
                "base",
                "-L",
                "theirs (remote)",
                str(ours_file),
                str(base_file),
                str(theirs_file),
            ],
            capture_output=True,
            text=True,
        )

        merged_content = ours_file.read_text()

        # merge-file returns:
        # 0 = clean merge
        # positive = number of conflicts
        # negative = error
        if result.returncode < 0:
            # Error occurred - treat as conflict to be safe
            # This preserves local content and flags for manual review
            has_conflicts = True
        else:
            has_conflicts = result.returncode > 0

        return merged_content, has_conflicts


def show_pull_summary(git_root, subtree_prefix, branch):
    """Show a summary of commits that will be pulled."""
    click.echo("Fetching remote to check for updates...")

    # Fetch the remote branch
    fetch_result = run_git_command(
        ["git", "fetch", REMOTE_URL, branch],
        cwd=git_root,
        check=False,
    )

    if fetch_result.returncode != 0:
        click.echo("⚠ Could not fetch remote to show summary", err=True)
        return True  # Continue anyway

    # First check: is there any actual content difference?
    # This handles cases where commit history diverged but content is the same,
    # or where local has changes not yet pushed to remote
    diff_result = run_git_command(
        ["git", "diff", "--stat", f"HEAD:{subtree_prefix}", "FETCH_HEAD"],
        cwd=git_root,
        check=False,
    )

    if diff_result.returncode == 0 and not diff_result.stdout.strip():
        # No content difference - we're up to date
        click.echo("\n✓ Already up to date - no content changes to pull")
        return False

    # Get local subtree split SHA to compare commits
    local_sha = get_subtree_split_sha(git_root, subtree_prefix)

    if not local_sha:
        click.echo("⚠ Could not determine local subtree state", err=True)
        return True  # Continue anyway

    # Find commits in remote that aren't in local subtree
    # We compare FETCH_HEAD (remote) with the local split
    log_result = run_git_command(
        ["git", "log", "--oneline", f"{local_sha}..FETCH_HEAD"],
        cwd=git_root,
        check=False,
    )

    if log_result.returncode != 0:
        # Fallback: just show recent remote commits
        click.echo("Recent commits on remote:")
        commits = get_remote_commits(git_root, branch, limit=10)
        if commits:
            for commit in commits[:10]:
                click.echo(f"  {commit}")
        return True

    incoming = log_result.stdout.strip()
    if incoming:
        lines = incoming.split("\n")
        click.echo(f"\n📥 {len(lines)} commit(s) to pull:")
        for line in lines[:15]:
            click.echo(f"  {line}")
        if len(lines) > 15:
            click.echo(f"  ... and {len(lines) - 15} more")
        click.echo("")
        return True
    else:
        click.echo("\n✓ Already up to date - no new commits to pull")
        return False


def get_tree_sha(git_root, commit_sha):
    """Get the tree SHA for a commit."""
    result = run_git_command(
        ["git", "rev-parse", f"{commit_sha}^{{tree}}"],
        cwd=git_root,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return None


def show_push_summary(git_root, subtree_prefix, branch):
    """Show a summary of commits that will be pushed."""
    click.echo("Analyzing changes to push...")

    # Get local subtree split SHA (must use actual split, not trailer, for push)
    local_sha = get_subtree_split_sha(git_root, subtree_prefix, for_push=True)

    if not local_sha:
        click.echo("⚠ Could not determine local subtree state", err=True)
        return True  # Continue anyway

    # Fetch the remote branch
    fetch_result = run_git_command(
        ["git", "fetch", REMOTE_URL, branch],
        cwd=git_root,
        check=False,
    )

    if fetch_result.returncode != 0:
        # Branch might not exist yet - that's OK for a new branch
        click.echo(f"Note: Branch '{branch}' may not exist on remote yet (will be created)")
        # Show recent subtree commits instead
        log_result = run_git_command(
            ["git", "log", "--oneline", "-10", local_sha],
            cwd=git_root,
            check=False,
        )
        if log_result.returncode == 0 and log_result.stdout.strip():
            click.echo("\nRecent subtree commits:")
            for line in log_result.stdout.strip().split("\n"):
                click.echo(f"  {line}")
        click.echo("")
        return True

    # Find commits in local subtree that aren't in remote
    log_result = run_git_command(
        ["git", "log", "--oneline", f"FETCH_HEAD..{local_sha}"],
        cwd=git_root,
        check=False,
    )

    if log_result.returncode != 0:
        click.echo("⚠ Could not determine outgoing commits", err=True)
        return True

    outgoing = log_result.stdout.strip()
    if outgoing:
        lines = outgoing.split("\n")

        # Check if trees are identical (merge commits with no actual changes)
        local_tree = get_tree_sha(git_root, local_sha)
        remote_tree = get_tree_sha(git_root, "FETCH_HEAD")

        if local_tree and remote_tree and local_tree == remote_tree:
            click.echo("\n✓ No changes to push - tree content is identical to remote")
            click.echo(f"  ({len(lines)} commit(s) exist but contain no file changes)")
            return False

        click.echo(f"\n📤 {len(lines)} commit(s) to push:")
        for line in lines[:15]:
            click.echo(f"  {line}")
        if len(lines) > 15:
            click.echo(f"  ... and {len(lines) - 15} more")

        # Show file changes diff stat
        diff_result = run_git_command(
            ["git", "diff", "--stat", f"FETCH_HEAD..{local_sha}"],
            cwd=git_root,
            check=False,
        )
        if diff_result.returncode == 0 and diff_result.stdout.strip():
            click.echo("\nFile changes:")
            for line in diff_result.stdout.strip().split("\n")[-20:]:
                click.echo(f"  {line}")

        click.echo("")
        return True
    else:
        click.echo("\n✓ No new commits to push - remote is up to date")
        return False


# Patterns that indicate migration-sensitive changes
# These are relative to the subtree root
MIGRATION_SENSITIVE_PATTERNS = [
    # Migration instructions themselves
    "CLAUDE.md",
    "README.md",
    # CI/CD workflows that apps may need to update
    "mcp_scripts/templates/ci/",
    # Shared packages - changes may require app updates
    "packages/mcp_auth/",
    "packages/mcp_middleware/",
    "packages/mcp_cache/",
    "packages/mcp_testing/",
    # UI templates - regeneration may be needed
    "ui_generator/templates/",
    # Core scripts that apps wrap
    "mcp_scripts/generate_ui.py",
    "mcp_scripts/regenerate_ui.py",
    "mcp_scripts/validate_mcp_tools.py",
]


def check_migration_sensitive_changes(git_root, subtree_prefix, head_before, head_after):
    """Check if any migration-sensitive files were changed in the pull.

    Args:
        git_root: Path to git repository root
        subtree_prefix: Path to subtree directory (relative to git_root)
        head_before: Commit SHA before the pull
        head_after: Commit SHA after the pull

    Returns:
        Tuple of (changed_files, categories) where:
        - changed_files: List of changed file paths (relative to subtree)
        - categories: Dict mapping category names to lists of changed files
    """
    if head_before == head_after:
        return [], {}

    # Get list of files changed in the pull
    diff_result = run_git_command(
        ["git", "diff", "--name-only", head_before, head_after, "--", subtree_prefix],
        cwd=git_root,
        check=False,
    )

    if diff_result.returncode != 0 or not diff_result.stdout.strip():
        return [], {}

    all_changed = diff_result.stdout.strip().split("\n")

    # Filter to migration-sensitive files
    prefix_len = len(subtree_prefix) + 1  # +1 for trailing slash
    sensitive_files = []
    categories = {
        "Migration Instructions": [],
        "CI/CD Workflows": [],
        "Shared Packages": [],
        "UI Templates": [],
        "Core Scripts": [],
    }

    for file_path in all_changed:
        # Get path relative to subtree
        if file_path.startswith(subtree_prefix + "/"):
            rel_path = file_path[prefix_len:]
        else:
            rel_path = file_path

        # Check if this file matches any sensitive pattern
        for pattern in MIGRATION_SENSITIVE_PATTERNS:
            if rel_path == pattern or rel_path.startswith(pattern):
                sensitive_files.append(rel_path)

                # Categorize the change
                if pattern in ("CLAUDE.md", "README.md"):
                    categories["Migration Instructions"].append(rel_path)
                elif "templates/ci/" in pattern:
                    categories["CI/CD Workflows"].append(rel_path)
                elif pattern.startswith("packages/"):
                    categories["Shared Packages"].append(rel_path)
                elif "ui_generator/templates/" in pattern:
                    categories["UI Templates"].append(rel_path)
                elif pattern.endswith(".py"):
                    categories["Core Scripts"].append(rel_path)
                break

    # Remove empty categories
    categories = {k: v for k, v in categories.items() if v}

    return sensitive_files, categories


def generate_migration_prompt(git_root, subtree_prefix, categories, head_before, head_after):
    """Generate a prompt for Claude to analyze migration-sensitive changes.

    Args:
        git_root: Path to git repository root
        subtree_prefix: Path to subtree directory (relative to git_root)
        categories: Dict mapping category names to lists of changed files
        head_before: Commit SHA before the pull
        head_after: Commit SHA after the pull

    Returns:
        A formatted prompt string
    """
    # Build a summary of what changed
    summary_parts = []
    for category, files in categories.items():
        summary_parts.append(f"- {category}: {len(files)} file(s)")

    summary = "\n".join(summary_parts)

    # Get the commit messages from the pull
    log_result = run_git_command(
        ["git", "log", "--oneline", f"{head_before}..{head_after}", "--", subtree_prefix],
        cwd=git_root,
        check=False,
    )
    if log_result.returncode == 0:
        commit_messages = log_result.stdout.strip()
    else:
        commit_messages = "(unable to get commit log)"

    prompt = f"""I just pulled updates to mercor-mcp-shared in my repository at:
{git_root}

The subtree is located at: {subtree_prefix}

The following migration-sensitive files were changed:
{summary}

Recent commits in this pull:
{commit_messages}

Please analyze the changes between commits {head_before[:8]}..{head_after[:8]} in the subtree
and determine if any updates are needed in my application code. Specifically:

1. Check CLAUDE.md for any new migration steps or changed instructions
2. If CI/CD workflows changed, compare my .github/workflows/ with the templates
3. If shared packages changed (mcp_auth, mcp_middleware, etc.), check if my code
   uses any modified APIs
4. If UI templates changed, determine if I need to regenerate my UI

For each change that requires action, provide specific instructions on what I need to update."""

    return prompt


def show_migration_prompt_if_needed(git_root, subtree_prefix, head_before):
    """Check for migration-sensitive changes and display prompt if needed.

    Args:
        git_root: Path to git repository root
        subtree_prefix: Path to subtree directory (relative to git_root)
        head_before: Commit SHA before the pull
    """
    final_head = run_git_command(
        ["git", "rev-parse", "HEAD"],
        cwd=git_root,
        check=False,
    ).stdout.strip()

    _, categories = check_migration_sensitive_changes(
        git_root, subtree_prefix, head_before, final_head
    )

    if categories:
        click.echo("")
        click.echo("=" * 70)
        click.echo("⚠️  MIGRATION-SENSITIVE CHANGES DETECTED")
        click.echo("=" * 70)
        click.echo("")
        click.echo("The following types of files were changed in this pull:")
        for category, files in categories.items():
            click.echo(f"  • {category}: {len(files)} file(s)")
        click.echo("")
        click.echo("These changes may require updates to your application code.")
        click.echo("")
        click.echo("-" * 70)
        click.echo("PASTE THE FOLLOWING PROMPT TO CLAUDE TO ANALYZE THE CHANGES:")
        click.echo("-" * 70)
        click.echo("")
        prompt = generate_migration_prompt(
            git_root, subtree_prefix, categories, head_before, final_head
        )
        click.echo(prompt)
        click.echo("")
        click.echo("-" * 70)


def refresh_package():
    """Reinstall mercor-mcp-shared and all its sub-packages to pick up changes.

    This function:
    1. Clears build directories and __pycache__ in the subtree
    2. Finds all packages defined in the subtree
    3. Reinstalls them using uv sync
    """
    click.echo("Refreshing mercor-mcp-shared packages...")

    git_root, subtree_prefix = get_subtree_paths()
    subtree_path = git_root / subtree_prefix

    # Step 1: Clear build directories and __pycache__
    click.echo("  Clearing build caches...")
    dirs_to_remove = ["build", "__pycache__", "*.egg-info"]
    removed_count = 0

    for pattern in dirs_to_remove:
        # rglob handles both glob patterns (*.egg-info) and exact names (build)
        for match in subtree_path.rglob(pattern):
            if match.is_dir():
                shutil.rmtree(match, ignore_errors=True)
                removed_count += 1

    if removed_count > 0:
        click.echo(f"  Removed {removed_count} cache director(ies)")

    # Step 2: Find all packages in the subtree (look for pyproject.toml files)
    packages = ["mercor-mcp-shared"]  # Always include the main package

    # Find sub-packages (packages/ directory pattern)
    packages_dir = subtree_path / "packages"
    if packages_dir.exists():
        for pkg_dir in packages_dir.iterdir():
            if pkg_dir.is_dir():
                pyproject = pkg_dir / "pyproject.toml"
                if pyproject.exists():
                    # Extract package name from pyproject.toml
                    try:
                        with open(pyproject, "rb") as f:
                            pkg_config = tomllib.load(f)
                        pkg_name = pkg_config.get("project", {}).get("name")
                        if pkg_name and pkg_name not in packages:
                            packages.append(pkg_name)
                    except Exception:
                        # If we can't parse it, try using directory name with underscores
                        pkg_name = pkg_dir.name.replace("-", "_")
                        if pkg_name not in packages:
                            packages.append(pkg_name)

    # Step 3: Build the uv sync command with all packages
    click.echo(f"  Reinstalling {len(packages)} package(s): {', '.join(packages)}")

    cmd = ["uv", "sync", "--all-extras"]
    for pkg in packages:
        cmd.extend(["--reinstall-package", pkg])

    result = subprocess.run(
        cmd,
        cwd=git_root,
        capture_output=False,  # Show output to user
        text=True,
    )

    if result.returncode == 0:
        click.echo("✓ Packages refreshed successfully")
    else:
        click.echo("✗ Failed to refresh packages", err=True)
        sys.exit(1)


@click.command()
def refresh():
    """
    Reinstall mercor-mcp-shared and all sub-packages to pick up local changes.

    This command:
    1. Clears build directories and __pycache__ in the subtree
    2. Finds all packages defined in the subtree (mcp_auth, mcp_middleware, etc.)
    3. Reinstalls them using 'uv sync --reinstall-package'

    Use this after pulling updates or making local changes to ensure the
    local environment reflects the latest code.
    """
    refresh_package()


def check_local_only_files(git_root, subtree_prefix):
    """Check for files that exist locally but not in the remote FETCH_HEAD.

    Distinguishes between:
    1. Truly local-only files (created locally, never in remote) - should preserve
    2. Files deleted by remote (existed at last merge, deleted in remote) - should NOT preserve

    Returns a tuple of (truly_local_only, deleted_by_remote) where each is a list
    of file paths relative to subtree.
    """
    # Use the upstream split SHA (from trailer) as the common ancestor.
    # This is more reliable than get_last_subtree_merge_commit which can
    # be confused by merge commits in the app repo history.
    # We use the trailer directly to avoid the git subtree split fallback,
    # which represents current local state and can't distinguish local vs upstream files.
    split_sha = get_subtree_split_from_trailer(git_root, subtree_prefix)

    # Get list of files in remote (FETCH_HEAD)
    remote_files_result = run_git_command(
        ["git", "ls-tree", "-r", "--name-only", "FETCH_HEAD"],
        cwd=git_root,
        check=False,
    )
    if remote_files_result.returncode != 0:
        return [], []  # Can't determine, proceed anyway

    remote_files = (
        set(remote_files_result.stdout.strip().split("\n"))
        if remote_files_result.stdout.strip()
        else set()
    )

    # Get list of files at the upstream split point (if available)
    base_files = set()
    if split_sha:
        base_files_result = run_git_command(
            ["git", "ls-tree", "-r", "--name-only", split_sha],
            cwd=git_root,
            check=False,
        )
        if base_files_result.returncode == 0 and base_files_result.stdout.strip():
            base_files = set(base_files_result.stdout.strip().split("\n"))

    # Get list of files in local subtree
    local_files_result = run_git_command(
        ["git", "ls-files", subtree_prefix],
        cwd=git_root,
        check=False,
    )
    if local_files_result.returncode != 0:
        return [], []

    local_files = set()
    for f in local_files_result.stdout.strip().split("\n"):
        if f:
            # Remove subtree prefix to compare with remote
            relative = f[len(subtree_prefix) :].lstrip("/")
            if relative:
                local_files.add(relative)

    # Find files that exist locally but not in remote
    local_not_in_remote = local_files - remote_files

    # Separate into truly local-only vs deleted by remote
    truly_local_only = []
    deleted_by_remote = []

    for f in sorted(local_not_in_remote):
        if f in base_files:
            # File existed at last merge but not in remote now = deleted by remote
            deleted_by_remote.append(f)
        else:
            # File didn't exist at last merge = truly local-only
            truly_local_only.append(f)

    return truly_local_only, deleted_by_remote


@click.command()
@click.argument("branch", default="main")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompt")
@click.option(
    "--preserve-local/--no-preserve-local",
    default=True,
    help="Preserve local-only files that don't exist in remote (default: preserve)",
)
def pull(branch, yes, preserve_local):
    """
    Pull updates from mercor-mcp-shared repository into the local subtree.

    BRANCH: The branch to pull from (default: main)

    By default, local-only files (files that exist locally but not in the remote)
    are preserved. Use --no-preserve-local to allow them to be deleted.
    """
    git_root, subtree_prefix = get_subtree_paths()

    click.echo("=== Pulling mercor-mcp-shared subtree updates ===")
    click.echo(f"Remote: {REMOTE_URL}")
    click.echo(f"Branch: {branch}")
    click.echo(f"Local path: {subtree_prefix}")
    click.echo("")

    # Show summary of what will be pulled
    has_updates = show_pull_summary(git_root, subtree_prefix, branch)

    if not has_updates:
        return  # Nothing to pull

    # Check for local-only files and files deleted by remote
    truly_local_only, deleted_by_remote = check_local_only_files(git_root, subtree_prefix)

    # Check for locally modified files by comparing the local subtree tree
    # against the upstream commit it was last synced to (from the trailer SHA).
    # This avoids false positives from merge commits that bring in history
    # without changing content, which git diff <commit>..HEAD would include.
    #
    # IMPORTANT: We use the trailer SHA directly, NOT get_subtree_split_sha(),
    # because the fallback (git subtree split) produces a SHA representing the
    # current local state — diffing that against HEAD:{prefix} would always be
    # empty, silently missing local modifications.
    split_sha = get_subtree_split_from_trailer(git_root, subtree_prefix)
    modified_files = []
    if split_sha:
        diff_result = run_git_command(
            ["git", "diff-tree", "--name-only", "-r", f"{split_sha}:", f"HEAD:{subtree_prefix}"],
            cwd=git_root,
            check=False,
        )
        if diff_result.returncode == 0 and diff_result.stdout.strip():
            # Paths from this diff are relative to subtree root; prefix them
            modified_files = [
                f"{subtree_prefix}/{f}" for f in diff_result.stdout.strip().split("\n") if f
            ]
    else:
        print(
            "  ⚠ No subtree trailer found — cannot detect local modifications.\n"
            "    If you have local changes in the subtree, back them up before proceeding."
        )

    # Build set of modified file paths (relative to subtree) for conflict detection
    modified_files_rel = set()
    for f in modified_files:
        if f.startswith(subtree_prefix + "/"):
            modified_files_rel.add(f[len(subtree_prefix) + 1 :])
        else:
            modified_files_rel.add(f)

    # Detect delete/modify conflicts: files deleted in remote but modified locally
    delete_modify_conflicts = [f for f in deleted_by_remote if f in modified_files_rel]
    # Files deleted in remote but NOT modified locally (safe to delete)
    safe_deletions = [f for f in deleted_by_remote if f not in modified_files_rel]

    # Show truly local-only files (will be preserved)
    if truly_local_only:
        if preserve_local:
            click.echo(f"📁 {len(truly_local_only)} local-only file(s) will be preserved:")
            for f in truly_local_only[:10]:
                click.echo(f"    {f}")
            if len(truly_local_only) > 10:
                click.echo(f"    ... and {len(truly_local_only) - 10} more")
            click.echo("")
        else:
            click.echo("⚠ Warning: The following local files don't exist in the remote branch")
            click.echo("  and will be DELETED during the pull (--no-preserve-local):")
            click.echo("")
            for f in truly_local_only[:20]:
                click.echo(f"    {f}")
            if len(truly_local_only) > 20:
                click.echo(f"    ... and {len(truly_local_only) - 20} more")
            click.echo("")

    # Show files that will be deleted (deleted in remote, not modified locally)
    if safe_deletions:
        click.echo(f"🗑️  {len(safe_deletions)} file(s) deleted in remote will be removed:")
        for f in safe_deletions[:10]:
            click.echo(f"    {f}")
        if len(safe_deletions) > 10:
            click.echo(f"    ... and {len(safe_deletions) - 10} more")
        click.echo("")

    # Show delete/modify conflicts
    if delete_modify_conflicts:
        click.echo(f"⚠️  {len(delete_modify_conflicts)} delete/modify conflict(s) detected:")
        click.echo("  (Files deleted in remote but modified locally)")
        for f in delete_modify_conflicts[:10]:
            click.echo(f"    {f}")
        if len(delete_modify_conflicts) > 10:
            click.echo(f"    ... and {len(delete_modify_conflicts) - 10} more")
        click.echo("")

    # Filter out local-only and deleted files from modified list (handled separately)
    local_only_set = set(f"{subtree_prefix}/{f}" for f in truly_local_only)
    deleted_set = set(f"{subtree_prefix}/{f}" for f in deleted_by_remote)
    modified_files = [f for f in modified_files if f not in local_only_set and f not in deleted_set]

    # Filter out files where local content already matches remote
    # (e.g., changes were pushed but local metadata doesn't reflect it)
    if modified_files:
        modified_files, already_synced = filter_already_synced_files(
            git_root, subtree_prefix, modified_files
        )
        if already_synced:
            click.echo(f"✓ {len(already_synced)} file(s) already match remote (no merge needed):")
            for f in already_synced[:10]:
                click.echo(f"    {f}")
            if len(already_synced) > 10:
                click.echo(f"    ... and {len(already_synced) - 10} more")
            click.echo("")

    if modified_files:
        click.echo(f"📝 {len(modified_files)} locally modified file(s) will be auto-merged:")
        for f in modified_files[:10]:
            click.echo(f"    {f}")
        if len(modified_files) > 10:
            click.echo(f"    ... and {len(modified_files) - 10} more")
        click.echo("")

    # Ask for confirmation unless -y flag is passed
    if not yes:
        if not click.confirm("Do you want to pull these changes?", default=True):
            click.echo("Pull cancelled.")
            return

    # Back up locally modified files for three-way merge.
    # Base content comes from the upstream split SHA (not an app repo commit)
    # so paths must be relative to the subtree root.
    modified_backup = {}
    if modified_files and split_sha:
        click.echo("Backing up locally modified files for auto-merge...")
        for file_path in modified_files:
            full_path = git_root / file_path
            if full_path.exists():
                # Skip binary files - they can't be three-way merged as text
                if is_binary_file(full_path):
                    click.echo(f"    Skipping binary file: {file_path}")
                    continue
                try:
                    # Base content path is relative to subtree root
                    rel_path = file_path
                    if rel_path.startswith(subtree_prefix + "/"):
                        rel_path = rel_path[len(subtree_prefix) + 1 :]
                    modified_backup[file_path] = {
                        "ours": full_path.read_text(),
                        "base": get_file_content_at_commit(git_root, split_sha, rel_path),
                    }
                except UnicodeDecodeError:
                    click.echo(f"    Skipping binary file: {file_path}")
                    continue

    # Back up files with delete/modify conflicts (for conflict resolution)
    delete_modify_backup = {}
    if delete_modify_conflicts and split_sha:
        click.echo("Backing up delete/modify conflict files...")
        for rel_path in delete_modify_conflicts:
            file_path = f"{subtree_prefix}/{rel_path}"
            full_path = git_root / file_path
            if full_path.exists():
                # Skip binary files - they can't be three-way merged as text
                if is_binary_file(full_path):
                    click.echo(f"    Skipping binary file: {file_path}")
                    continue
                try:
                    delete_modify_backup[file_path] = {
                        "ours": full_path.read_text(),
                        "base": get_file_content_at_commit(git_root, split_sha, rel_path),
                    }
                except UnicodeDecodeError:
                    click.echo(f"    Skipping binary file: {file_path}")
                    continue

    # Back up local-only files if preserving
    backup_dir = None
    if preserve_local and truly_local_only:
        click.echo("")
        click.echo("Backing up local-only files...")
        backup_dir = Path(tempfile.mkdtemp(prefix="shared-pull-backup-"))
        subtree_path = git_root / subtree_prefix
        for rel_path in truly_local_only:
            src = subtree_path / rel_path
            dst = backup_dir / rel_path
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)

    click.echo("")
    click.echo("Pulling changes...")

    # Save HEAD before pull to detect if a new commit was created
    head_before = run_git_command(
        ["git", "rev-parse", "HEAD"],
        cwd=git_root,
        check=False,
    ).stdout.strip()

    # Pull the changes from the remote repository
    result = run_git_command(
        ["git", "subtree", "pull", f"--prefix={subtree_prefix}", REMOTE_URL, branch, "--squash"],
        cwd=git_root,
        check=False,
    )

    # Check if a new commit was created
    head_after = run_git_command(
        ["git", "rev-parse", "HEAD"],
        cwd=git_root,
        check=False,
    ).stdout.strip()
    merge_created = head_before != head_after

    # Explicitly delete files that were deleted in remote (safe_deletions)
    # git subtree pull --squash may not handle these correctly
    # Do this regardless of pull return code - we've verified these are safe to delete
    deleted_count = 0
    if safe_deletions:
        click.echo("")
        click.echo("Removing files deleted in remote...")
        subtree_path = git_root / subtree_prefix
        for rel_path in safe_deletions:
            file_path = subtree_path / rel_path
            if file_path.exists():
                # Remove the file
                file_path.unlink()
                # Stage the deletion
                run_git_command(
                    ["git", "add", str(file_path)],
                    cwd=git_root,
                    check=False,
                )
                deleted_count += 1
        if deleted_count > 0:
            click.echo(f"✓ Removed {deleted_count} file(s) deleted in remote")

    if result.returncode == 0:
        click.echo("")
        click.echo(f"✓ Successfully pulled updates from {branch} branch")

        # Include deletions in the commit
        if deleted_count > 0:
            if merge_created:
                # Amend the merge commit to include deletions
                run_git_command(
                    ["git", "commit", "--amend", "--no-edit"],
                    cwd=git_root,
                    check=False,
                )
            else:
                # No merge commit was created, create a new commit for deletions
                run_git_command(
                    ["git", "commit", "-m", "Remove files deleted in remote"],
                    cwd=git_root,
                    check=False,
                )
                merge_created = True  # We now have a commit to amend

        # Restore backed-up local-only files
        if backup_dir and backup_dir.exists():
            click.echo("")
            click.echo("Restoring local-only files...")
            subtree_path = git_root / subtree_prefix
            restored_count = 0
            for rel_path in truly_local_only:
                src = backup_dir / rel_path
                dst = subtree_path / rel_path
                if src.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    # Stage the restored file
                    run_git_command(
                        ["git", "add", str(dst)],
                        cwd=git_root,
                        check=False,
                    )
                    restored_count += 1
            click.echo(f"✓ Restored {restored_count} local-only file(s)")

            # Include restored files in the commit
            if restored_count > 0:
                if merge_created:
                    # Amend the merge/deletion commit to include restored files
                    run_git_command(
                        ["git", "commit", "--amend", "--no-edit"],
                        cwd=git_root,
                        check=False,
                    )
                else:
                    # No commit yet, create one for restored files
                    run_git_command(
                        ["git", "commit", "-m", "Restore local-only files after pull"],
                        cwd=git_root,
                        check=False,
                    )

            # Clean up backup
            shutil.rmtree(backup_dir, ignore_errors=True)

        # Handle delete/modify conflicts (write conflict markers)
        if delete_modify_backup:
            click.echo("")
            click.echo("Creating conflict markers for delete/modify conflicts...")
            subtree_path = git_root / subtree_prefix
            for file_path, backup_data in delete_modify_backup.items():
                full_path = git_root / file_path
                # Create conflict file with our version and note about deletion
                # Build markers via concatenation to avoid git detecting them in source
                start_marker = "<" * 7 + " ours (local)"
                separator = "=" * 7
                end_marker = ">" * 7 + " theirs (remote)"
                # Ensure content ends with newline for proper conflict marker formatting
                ours_content = backup_data["ours"]
                if not ours_content.endswith("\n"):
                    ours_content += "\n"
                conflict_content = f"""{start_marker}
{ours_content}{separator}
[File was deleted in remote]
{end_marker}
"""
                full_path.parent.mkdir(parents=True, exist_ok=True)
                full_path.write_text(conflict_content)
                # Don't stage - leave as unstaged conflict
            click.echo(f"⚠ Created {len(delete_modify_backup)} delete/modify conflict file(s)")
            click.echo("  To keep local version: edit file to remove markers, then git add <file>")
            click.echo("  To accept deletion:    rm <file>")

        # Three-way merge locally modified files
        if modified_backup:
            click.echo("")
            click.echo("Auto-merging locally modified files...")
            merge_success = 0
            merge_conflicts = 0
            files_with_conflicts = []

            for file_path, backup_data in modified_backup.items():
                full_path = git_root / file_path
                if not full_path.exists():
                    # File was deleted in remote, keep our version
                    full_path.parent.mkdir(parents=True, exist_ok=True)
                    full_path.write_text(backup_data["ours"])
                    run_git_command(["git", "add", str(full_path)], cwd=git_root, check=False)
                    merge_success += 1
                    continue

                # Get the new remote version (theirs)
                # Handle case where remote introduced binary/invalid UTF-8 content
                if is_binary_file(full_path):
                    click.echo(f"    Skipping binary file (remote version): {file_path}")
                    # Keep our version since we can't merge binary
                    full_path.write_text(backup_data["ours"])
                    run_git_command(["git", "add", str(full_path)], cwd=git_root, check=False)
                    merge_success += 1
                    continue

                try:
                    theirs_content = full_path.read_text()
                except UnicodeDecodeError:
                    click.echo(f"    Skipping binary file (remote version): {file_path}")
                    # Keep our version since we can't merge binary
                    full_path.write_text(backup_data["ours"])
                    run_git_command(["git", "add", str(full_path)], cwd=git_root, check=False)
                    merge_success += 1
                    continue

                # If theirs == ours, nothing changed for this file
                if theirs_content == backup_data["ours"]:
                    merge_success += 1
                    continue

                # Perform three-way merge
                merged_content, has_conflicts = merge_file_threeway(
                    base_content=backup_data["base"],
                    ours_content=backup_data["ours"],
                    theirs_content=theirs_content,
                    file_name=Path(file_path).name,
                )

                # Write merged content
                full_path.write_text(merged_content)

                if has_conflicts:
                    merge_conflicts += 1
                    files_with_conflicts.append(file_path)
                else:
                    run_git_command(["git", "add", str(full_path)], cwd=git_root, check=False)
                    merge_success += 1

            if merge_success > 0:
                click.echo(f"✓ Auto-merged {merge_success} file(s) successfully")

            if merge_conflicts > 0:
                click.echo(f"⚠ {merge_conflicts} file(s) have merge conflicts:")
                for f in files_with_conflicts:
                    click.echo(f"    {f}")
                click.echo("")
                click.echo("Please resolve the conflicts manually, then run:")
                click.echo("  git add <resolved-files>")
                click.echo("  git commit --amend --no-edit")
            else:
                # Amend the merge commit to include merged files
                run_git_command(
                    ["git", "commit", "--amend", "--no-edit"],
                    cwd=git_root,
                    check=False,
                )

            # Clean up backup (only if backup_dir was set)
            if backup_dir is not None:
                shutil.rmtree(backup_dir, ignore_errors=True)

        click.echo("")
        # Refresh package to pick up changes
        refresh_package()

        # Check for migration-sensitive changes and show prompt if needed
        show_migration_prompt_if_needed(git_root, subtree_prefix, head_before)
    else:
        click.echo("")
        # Check if this is a merge conflict
        status_result = run_git_command(
            ["git", "status", "--porcelain"],
            cwd=git_root,
            check=False,
        )

        # Parse conflict types from porcelain output
        # UU = both modified, AA = both added, DD = both deleted
        # UD = deleted by them (exists local, deleted remote)
        # DU = deleted by us (deleted local, exists remote)
        # AU = added by us, UA = added by them
        conflict_types = {
            "both_modified": [],
            "deleted_by_them": [],
            "deleted_by_us": [],
            "other_conflicts": [],
        }

        for line in status_result.stdout.split("\n"):
            if not line or len(line) < 3:
                continue
            status_code = line[:2]
            file_path = line[3:]

            if status_code == "UU":
                conflict_types["both_modified"].append(file_path)
            elif status_code == "UD":
                conflict_types["deleted_by_them"].append(file_path)
            elif status_code == "DU":
                conflict_types["deleted_by_us"].append(file_path)
            elif status_code in ("AA", "DD", "AU", "UA"):
                conflict_types["other_conflicts"].append(file_path)

        has_conflicts = any(files for files in conflict_types.values())

        if has_conflicts or "CONFLICT" in (result.stdout + result.stderr):
            click.echo("⚠ Merge conflict detected!", err=True)
            click.echo("")

            # Show conflict details by type
            if conflict_types["deleted_by_them"]:
                count = len(conflict_types["deleted_by_them"])
                click.echo(f"Files deleted in remote but exist locally ({count}):")
                for f in conflict_types["deleted_by_them"][:10]:
                    click.echo(f"  {f}")
                if len(conflict_types["deleted_by_them"]) > 10:
                    click.echo(f"  ... and {len(conflict_types['deleted_by_them']) - 10} more")
                click.echo("")

            if conflict_types["both_modified"]:
                click.echo(f"Files modified in both ({len(conflict_types['both_modified'])}):")
                for f in conflict_types["both_modified"][:10]:
                    click.echo(f"  {f}")
                if len(conflict_types["both_modified"]) > 10:
                    click.echo(f"  ... and {len(conflict_types['both_modified']) - 10} more")
                click.echo("")

            if conflict_types["deleted_by_us"]:
                count = len(conflict_types["deleted_by_us"])
                click.echo(f"Files deleted locally but exist in remote ({count}):")
                for f in conflict_types["deleted_by_us"][:10]:
                    click.echo(f"  {f}")
                if len(conflict_types["deleted_by_us"]) > 10:
                    click.echo(f"  ... and {len(conflict_types['deleted_by_us']) - 10} more")
                click.echo("")

            if conflict_types["other_conflicts"]:
                click.echo(f"Other conflicts ({len(conflict_types['other_conflicts'])}):")
                for f in conflict_types["other_conflicts"][:10]:
                    click.echo(f"  {f}")
                click.echo("")

            # Use pre-computed truly_local_only and deleted_by_remote from earlier
            # Convert to full paths for conflict resolution
            truly_local_only_set = set(truly_local_only)  # relative paths
            prefix_len = len(subtree_prefix) + 1  # +1 for the trailing slash

            # Categorize "deleted by them" conflicts
            local_only_conflicts = []
            deleted_conflicts = []
            for fp in conflict_types["deleted_by_them"]:
                # Strip the subtree prefix to get the relative path
                rel_path = fp[prefix_len:] if fp.startswith(subtree_prefix + "/") else fp
                if rel_path in truly_local_only_set:
                    local_only_conflicts.append(fp)
                else:
                    deleted_conflicts.append(fp)

            # Auto-resolve truly local-only files (always keep them)
            if preserve_local and local_only_conflicts:
                click.echo("Auto-resolving local-only files (keeping them)...")
                for file_path in local_only_conflicts:
                    run_git_command(
                        ["git", "checkout", "--ours", "--", file_path],
                        cwd=git_root,
                        check=False,
                    )
                    run_git_command(
                        ["git", "add", file_path],
                        cwd=git_root,
                        check=False,
                    )
                click.echo(f"✓ Kept {len(local_only_conflicts)} local-only file(s)")
                click.echo("")

            # Check if there are remaining conflicts that need manual resolution
            remaining_conflicts = (
                conflict_types["both_modified"]
                or deleted_conflicts
                or conflict_types["deleted_by_us"]
                or conflict_types["other_conflicts"]
            )

            if not remaining_conflicts:
                # All conflicts were local-only files, complete the merge
                commit_result = run_git_command(
                    ["git", "commit", "--no-edit"],
                    cwd=git_root,
                    check=False,
                )

                if commit_result.returncode == 0:
                    click.echo(f"✓ Successfully pulled updates from {branch} branch")
                    click.echo("  (Local-only files were preserved)")
                    click.echo("")
                    if backup_dir and backup_dir.exists():
                        shutil.rmtree(backup_dir, ignore_errors=True)
                    refresh_package()

                    # Check for migration-sensitive changes
                    show_migration_prompt_if_needed(git_root, subtree_prefix, head_before)
                    return
                else:
                    click.echo("✗ Failed to complete merge after resolving conflicts", err=True)

            # Show remaining conflicts that need manual resolution
            if deleted_conflicts:
                click.echo(
                    f"Files deleted in remote (need manual decision) ({len(deleted_conflicts)}):"
                )
                for f in deleted_conflicts[:10]:
                    click.echo(f"  {f}")
                if len(deleted_conflicts) > 10:
                    click.echo(f"  ... and {len(deleted_conflicts) - 10} more")
                click.echo("")

            # Tell user about backup if it exists
            if backup_dir and backup_dir.exists():
                click.echo(f"📁 Local-only files backed up to: {backup_dir}")
                click.echo(
                    "   (Will be restored after you resolve conflicts and run shared-pull again)"
                )
                click.echo("")

            # For "both modified" files, extract base/ours/theirs for three-way merge
            if conflict_types["both_modified"]:
                merge_dir = Path(tempfile.mkdtemp(prefix="shared-pull-merge-"))
                click.echo(f"📂 Three-way merge files saved to: {merge_dir}")
                click.echo("")

                for file_path in conflict_types["both_modified"]:
                    file_name = Path(file_path).name
                    base_file = merge_dir / f"{file_name}.base"
                    ours_file = merge_dir / f"{file_name}.ours"
                    theirs_file = merge_dir / f"{file_name}.theirs"

                    # Extract base version (stage 1)
                    base_result = run_git_command(
                        ["git", "show", f":1:{file_path}"],
                        cwd=git_root,
                        check=False,
                    )
                    if base_result.returncode == 0:
                        base_file.write_text(base_result.stdout)

                    # Extract ours version (stage 2)
                    ours_result = run_git_command(
                        ["git", "show", f":2:{file_path}"],
                        cwd=git_root,
                        check=False,
                    )
                    if ours_result.returncode == 0:
                        ours_file.write_text(ours_result.stdout)

                    # Extract theirs version (stage 3)
                    theirs_result = run_git_command(
                        ["git", "show", f":3:{file_path}"],
                        cwd=git_root,
                        check=False,
                    )
                    if theirs_result.returncode == 0:
                        theirs_file.write_text(theirs_result.stdout)

                    click.echo(f"  {file_name}:")
                    click.echo(f"    base:   {base_file}")
                    click.echo(f"    ours:   {ours_file}")
                    click.echo(f"    theirs: {theirs_file}")

                click.echo("")

            click.echo("To resolve remaining conflicts:")
            if conflict_types["both_modified"]:
                click.echo("  • VS Code: Open file and use the merge editor (auto-detected)")
                click.echo("  • Or run: git mergetool")
            if deleted_conflicts:
                click.echo("  • To keep local version: git checkout --ours -- <file>")
                click.echo("  • To accept remote deletion: git rm <file>")
            click.echo("  • Finally: git commit")
            click.echo("")
            click.echo("Or to abort the merge:")
            click.echo("  git merge --abort")
            sys.exit(1)
        else:
            # Clean up backup on non-conflict failure
            if backup_dir and backup_dir.exists():
                shutil.rmtree(backup_dir, ignore_errors=True)
            click.echo("✗ Failed to pull updates", err=True)
            if result.stderr:
                click.echo(result.stderr, err=True)
            sys.exit(1)


@click.command()
@click.argument("branch", required=True)
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompt")
def push(branch, yes):
    """
    Push local changes from the subtree back to mercor-mcp-shared repository.

    BRANCH: The target branch to push to (required - never push directly to main)
    """
    git_root, subtree_prefix = get_subtree_paths()

    click.echo("=== Pushing mercor-mcp-shared subtree changes ===")
    click.echo(f"Remote: {REMOTE_URL}")
    click.echo(f"Branch: {branch}")
    click.echo(f"Local path: {subtree_prefix}")
    click.echo("")

    # Check if there are any uncommitted changes in the subtree directory
    diff_result = run_git_command(
        ["git", "diff", "--quiet", "HEAD", "--", subtree_prefix],
        cwd=git_root,
        check=False,
    )
    cached_result = run_git_command(
        ["git", "diff", "--cached", "--quiet", "--", subtree_prefix],
        cwd=git_root,
        check=False,
    )

    if diff_result.returncode != 0 or cached_result.returncode != 0:
        click.echo(f"⚠ Warning: You have uncommitted changes in {subtree_prefix}", err=True)
        click.echo(
            "It's recommended to commit your changes first before pushing to the remote.",
            err=True,
        )
        if not click.confirm("Do you want to continue anyway?", default=False):
            click.echo("Push cancelled.")
            sys.exit(0)

    # Show summary of what will be pushed
    has_changes = show_push_summary(git_root, subtree_prefix, branch)

    if not has_changes:
        return  # Nothing to push

    # Ask for confirmation unless -y flag is passed
    if not yes:
        if not click.confirm("Do you want to push these changes?", default=True):
            click.echo("Push cancelled.")
            return

    # Push the changes to the remote repository
    click.echo("")
    click.echo(f"Pushing changes to {branch} branch...")
    click.echo("(This may take a while as git reconstructs subtree history...)")
    click.echo("")

    result = run_git_command(
        ["git", "subtree", "push", f"--prefix={subtree_prefix}", REMOTE_URL, branch],
        cwd=git_root,
        check=False,
    )

    if result.returncode == 0:
        click.echo("")
        click.echo(f"✓ Successfully pushed changes to {branch} branch")
    else:
        click.echo("")
        click.echo("✗ Failed to push changes", err=True)

        # Combine stdout and stderr for error detection
        output = (result.stdout or "") + (result.stderr or "")

        # Detect specific error types and provide guidance
        if "non-fast-forward" in output or "rejected" in output:
            click.echo("")
            click.echo("The remote branch has commits that aren't in your local subtree.", err=True)
            click.echo("")
            click.echo("To resolve this:", err=True)
            click.echo(f"  1. Pull remote changes first:  shared-pull {branch}", err=True)
            click.echo("  2. Resolve any conflicts if needed", err=True)
            click.echo(f"  3. Try pushing again:  shared-push {branch}", err=True)
        elif "Permission denied" in output or "could not read Username" in output:
            click.echo("")
            click.echo("Authentication failed. Check your GitHub credentials.", err=True)
        elif result.stderr:
            # Show the raw error for other cases
            click.echo("")
            click.echo("Error details:", err=True)
            click.echo(result.stderr, err=True)

        sys.exit(1)


@click.command()
@click.argument("branch", required=True)
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompt")
def switch(branch, yes):
    """
    Switch the subtree to a different branch (replaces content, does not merge).

    BRANCH: The branch to switch to (required)

    Examples:
      shared-switch develop
      shared-switch feature/new-feature
    """
    git_root, subtree_prefix = get_subtree_paths()

    click.echo("=== Switching mercor-mcp-shared subtree to different branch ===")
    click.echo(f"Remote: {REMOTE_URL}")
    click.echo(f"Target branch: {branch}")
    click.echo(f"Local path: {subtree_prefix}")
    click.echo("")

    # Check for uncommitted changes in the subtree
    diff_result = run_git_command(
        ["git", "diff", "--quiet", "HEAD", "--", subtree_prefix],
        cwd=git_root,
        check=False,
    )
    cached_result = run_git_command(
        ["git", "diff", "--cached", "--quiet", "--", subtree_prefix],
        cwd=git_root,
        check=False,
    )

    if diff_result.returncode != 0 or cached_result.returncode != 0:
        click.echo(f"⚠ Warning: You have uncommitted changes in {subtree_prefix}")
        click.echo("Please commit or stash your changes first before switching branches.")
        sys.exit(1)

    # Fetch the target branch
    click.echo(f"Fetching {branch} from remote...")
    fetch_result = run_git_command(
        ["git", "fetch", REMOTE_URL, branch],
        cwd=git_root,
        check=False,
    )

    if fetch_result.returncode != 0:
        click.echo(f"✗ Failed to fetch branch {branch}")
        sys.exit(1)

    # Show recent commits on the target branch
    log_result = run_git_command(
        ["git", "log", "--oneline", "-10", "FETCH_HEAD"],
        cwd=git_root,
        check=False,
    )
    if log_result.returncode == 0 and log_result.stdout.strip():
        click.echo(f"\nRecent commits on {branch}:")
        for line in log_result.stdout.strip().split("\n"):
            click.echo(f"  {line}")
        click.echo("")

    # Ask for confirmation unless -y flag is passed
    if not yes:
        click.echo("⚠ This will replace the entire subtree content (not a merge).")
        if not click.confirm("Do you want to switch to this branch?", default=True):
            click.echo("Switch cancelled.")
            return

    click.echo(f"Replacing subtree content with {branch}...")

    # Remove the current subtree content
    run_git_command(
        ["git", "rm", "-r", subtree_prefix],
        cwd=git_root,
    )

    # Read the new branch content into the subtree location
    run_git_command(
        ["git", "read-tree", f"--prefix={subtree_prefix}", "-u", "FETCH_HEAD"],
        cwd=git_root,
    )

    # Commit the switch
    commit_msg = f"""Switch subtree to {branch} branch

Updated {subtree_prefix} to match {branch} from {REMOTE_URL}

This is a clean branch switch (not a merge)."""

    result = run_git_command(
        ["git", "commit", "-m", commit_msg],
        cwd=git_root,
        check=False,
    )

    if result.returncode == 0:
        click.echo("")
        click.echo(f"✓ Successfully switched subtree to {branch} branch")
        click.echo("")
        click.echo(
            "Note: This was a clean switch (not a merge). To push changes back to this branch,"
        )
        click.echo(f"use: shared-push {branch}")
        refresh_package()
    else:
        click.echo("")
        click.echo("✗ Failed to commit branch switch", err=True)
        sys.exit(1)


# For backward compatibility or direct execution
@click.group()
def cli():
    """Manage mercor-mcp-shared git subtree."""
    pass


cli.add_command(pull)
cli.add_command(push)
cli.add_command(switch)
cli.add_command(refresh)


if __name__ == "__main__":
    cli()
