#!/usr/bin/env python
"""Validate that a repository has been correctly migrated to mercor-mcp-shared.

Checks performed:
- mercor-mcp-shared is listed as a dependency in pyproject.toml
- Old local packages (mcp_auth, mcp_cache, etc.) have been removed
- Old ui_generator/ and templates/ directories have been removed
- Wrapper scripts in scripts/ point to valid mcp_scripts modules
- Python imports from shared packages resolve correctly
- CI workflows have git auth configured for subtree access
- MCP server end_user_documentation/ exists if wiki/ was present
"""

import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]


@dataclass
class CheckResult:
    """Result of a single validation check."""

    name: str
    passed: bool
    message: str
    details: list[str] = field(default_factory=list)


@dataclass
class ValidationReport:
    """Full validation report."""

    checks: list[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for c in self.checks if c.passed)

    @property
    def failed(self) -> int:
        return sum(1 for c in self.checks if not c.passed)

    @property
    def all_passed(self) -> bool:
        return self.failed == 0


# Old directories that should be removed after migration
OLD_DIRECTORIES = [
    "packages/mcp_auth",
    "packages/mcp_cache",
    "packages/mcp_middleware",
    "packages/mcp_testing",
    "packages/mcp_schema",
    "ui_generator",
    "templates",
]

# Old script files that should be replaced by wrappers
OLD_SCRIPT_FILES = [
    "scripts/db_tools.py",
    "scripts/generate_guide_json.py",
]

# Wrapper structural markers
WRAPPER_MARKERS = [
    "Implementation: mercor-mcp-shared",
    "sys.modules[__name__] =",
    "from mcp_scripts import",
]


def find_subtree_path(repo_root: Path) -> Path | None:
    """Find the mercor-mcp-shared subtree path within the repo."""
    pyproject = repo_root / "pyproject.toml"
    if not pyproject.exists():
        return None

    try:
        data = tomllib.loads(pyproject.read_text())
        sources = data.get("tool", {}).get("uv", {}).get("sources", {})
        shared_source = sources.get("mercor-mcp-shared", {})
        if "path" in shared_source:
            candidate = repo_root / shared_source["path"]
            if candidate.exists():
                return candidate
    except Exception:
        pass

    # Fallback to common location
    candidate = repo_root / "packages" / "mercor-mcp-shared"
    if candidate.exists():
        return candidate

    return None


def check_dependency(repo_root: Path) -> CheckResult:
    """Check that mercor-mcp-shared is listed as a dependency."""
    pyproject = repo_root / "pyproject.toml"
    if not pyproject.exists():
        return CheckResult(
            name="mercor-mcp-shared dependency",
            passed=False,
            message="pyproject.toml not found",
        )

    try:
        data = tomllib.loads(pyproject.read_text())
    except Exception as e:
        return CheckResult(
            name="mercor-mcp-shared dependency",
            passed=False,
            message=f"Failed to parse pyproject.toml: {e}",
        )

    deps = data.get("project", {}).get("dependencies", [])
    has_dep = any("mercor-mcp-shared" in d for d in deps)

    sources = data.get("tool", {}).get("uv", {}).get("sources", {})
    has_source = "mercor-mcp-shared" in sources

    if has_dep and has_source:
        source_info = sources["mercor-mcp-shared"]
        return CheckResult(
            name="mercor-mcp-shared dependency",
            passed=True,
            message=f"Listed as dependency with source: {source_info}",
        )
    elif has_dep:
        return CheckResult(
            name="mercor-mcp-shared dependency",
            passed=False,
            message="Listed in dependencies but missing from [tool.uv.sources]",
        )
    else:
        return CheckResult(
            name="mercor-mcp-shared dependency",
            passed=False,
            message="Not found in project dependencies",
        )


def check_old_directories(repo_root: Path) -> CheckResult:
    """Check that old local package directories have been removed."""
    remaining = []
    for dir_path in OLD_DIRECTORIES:
        full_path = repo_root / dir_path
        if full_path.exists() and full_path.is_dir():
            # Count files to show scope
            file_count = sum(1 for _ in full_path.rglob("*") if _.is_file())
            remaining.append(f"{dir_path}/ ({file_count} files)")

    if remaining:
        return CheckResult(
            name="Old directories removed",
            passed=False,
            message=f"{len(remaining)} old directories still present",
            details=remaining,
        )

    return CheckResult(
        name="Old directories removed",
        passed=True,
        message="All old shared directories have been removed",
    )


def check_old_scripts(repo_root: Path) -> CheckResult:
    """Check that old script files have been removed or replaced."""
    remaining = []
    for script_path in OLD_SCRIPT_FILES:
        full_path = repo_root / script_path
        if full_path.exists():
            content = full_path.read_text()
            if not any(marker in content for marker in WRAPPER_MARKERS):
                remaining.append(f"{script_path} (not a wrapper)")

    if remaining:
        return CheckResult(
            name="Old scripts removed",
            passed=False,
            message=f"{len(remaining)} old scripts still present",
            details=remaining,
        )

    return CheckResult(
        name="Old scripts removed",
        passed=True,
        message="Old script files removed or replaced with wrappers",
    )


def check_wrappers(repo_root: Path) -> CheckResult:
    """Check that wrapper scripts point to valid mcp_scripts modules."""
    scripts_dir = repo_root / "scripts"
    if not scripts_dir.exists():
        return CheckResult(
            name="Wrapper scripts valid",
            passed=True,
            message="No scripts/ directory (nothing to validate)",
        )

    subtree = find_subtree_path(repo_root)
    if subtree is None:
        return CheckResult(
            name="Wrapper scripts valid",
            passed=False,
            message="Cannot find mercor-mcp-shared subtree to validate against",
        )

    mcp_scripts_dir = subtree / "mcp_scripts"
    broken = []
    valid = 0

    for script in sorted(scripts_dir.glob("*.py")):
        if script.name == "__init__.py":
            continue

        try:
            content = script.read_text()
        except OSError:
            continue

        # Only check files that look like wrappers
        if not all(marker in content for marker in WRAPPER_MARKERS):
            continue

        # Extract the module name from the import
        match = re.search(r"from mcp_scripts import (\w+)", content)
        if not match:
            broken.append(f"{script.name}: can't parse module import")
            continue

        module_name = match.group(1)
        module_path = mcp_scripts_dir / f"{module_name}.py"

        if not module_path.exists():
            broken.append(f"{script.name}: mcp_scripts/{module_name}.py not found")
        else:
            valid += 1

    if broken:
        return CheckResult(
            name="Wrapper scripts valid",
            passed=False,
            message=f"{len(broken)} broken wrappers (of {valid + len(broken)} total)",
            details=broken,
        )

    return CheckResult(
        name="Wrapper scripts valid",
        passed=True,
        message=f"All {valid} wrapper scripts point to valid modules",
    )


def check_imports(repo_root: Path) -> CheckResult:
    """Check that Python files don't import from old local packages directly."""
    mcp_servers = repo_root / "mcp_servers"
    if not mcp_servers.exists():
        return CheckResult(
            name="Import paths updated",
            passed=True,
            message="No mcp_servers/ directory",
        )

    # Patterns that indicate old-style imports from local packages
    old_import_patterns = [
        # Direct imports from packages that are now in mercor-mcp-shared
        r"from\s+packages\.",
        r"import\s+packages\.",
        # Imports from ui_generator (should use mercor-mcp-shared's)
        r"from\s+ui_generator\s+import",
    ]

    issues = []

    for py_file in mcp_servers.rglob("*.py"):
        try:
            content = py_file.read_text()
        except OSError:
            continue

        rel_path = py_file.relative_to(repo_root)

        for pattern in old_import_patterns:
            for match in re.finditer(pattern, content):
                line_num = content[: match.start()].count("\n") + 1
                line = content.splitlines()[line_num - 1].strip()
                issues.append(f"{rel_path}:{line_num}: {line}")

    if issues:
        return CheckResult(
            name="Import paths updated",
            passed=False,
            message=f"{len(issues)} old-style imports found",
            details=issues[:20],  # Cap at 20 to avoid noise
        )

    return CheckResult(
        name="Import paths updated",
        passed=True,
        message="No old-style package imports found in mcp_servers/",
    )


def check_ci_workflows(repo_root: Path) -> CheckResult:
    """Check that CI workflows are properly configured for subtree access.

    The mercor-mcp-shared subtree is accessed via local path, so workflows
    just need proper checkout configuration. We check for:
    - actions/checkout (handles auth automatically)
    - setup-uv action (for uv sync to work)
    """
    workflows_dir = repo_root / ".github" / "workflows"
    if not workflows_dir.exists():
        return CheckResult(
            name="CI workflows configured",
            passed=True,
            message="No .github/workflows/ directory",
        )

    issues = []
    checked = 0

    for workflow in sorted(workflows_dir.glob("*.yml")):
        try:
            content = workflow.read_text()
        except OSError:
            continue

        # Only check workflows that run uv sync (need checkout for subtree)
        if "uv sync" not in content:
            continue

        checked += 1
        workflow_issues = []

        # Check for checkout action (required to access subtree)
        if "actions/checkout" not in content:
            workflow_issues.append("missing actions/checkout")

        # Check for uv setup (recommended for uv sync)
        if "setup-uv" not in content and "astral-sh/setup-uv" not in content:
            # Also accept manual uv installation
            if "pip install uv" not in content and "pipx install uv" not in content:
                workflow_issues.append("missing uv setup")

        if workflow_issues:
            issues.append(f"{workflow.name}: {', '.join(workflow_issues)}")

    if issues:
        return CheckResult(
            name="CI workflows configured",
            passed=False,
            message=f"{len(issues)} of {checked} workflows have configuration issues",
            details=issues,
        )

    if checked == 0:
        return CheckResult(
            name="CI workflows configured",
            passed=True,
            message="No workflows with uv sync found",
        )

    return CheckResult(
        name="CI workflows configured",
        passed=True,
        message=f"All {checked} workflows with uv sync are properly configured",
    )


def check_wiki_migrated(repo_root: Path) -> CheckResult:
    """Check that wiki content has been migrated to end_user_documentation/."""
    wiki_dir = repo_root / "wiki"

    if not wiki_dir.exists():
        # Check if end_user_documentation exists for any server
        mcp_servers = repo_root / "mcp_servers"
        if mcp_servers.exists():
            servers_with_docs = []
            for server_dir in sorted(mcp_servers.iterdir()):
                if not server_dir.is_dir():
                    continue
                doc_dir = server_dir / "end_user_documentation"
                if doc_dir.exists():
                    doc_count = sum(1 for f in doc_dir.rglob("*") if f.is_file())
                    servers_with_docs.append(f"{server_dir.name}/ ({doc_count} files)")

            if servers_with_docs:
                return CheckResult(
                    name="Wiki content migrated",
                    passed=True,
                    message="wiki/ removed, end_user_documentation/ present",
                    details=servers_with_docs,
                )

        return CheckResult(
            name="Wiki content migrated",
            passed=True,
            message="No wiki/ directory (nothing to migrate)",
        )

    # wiki/ still exists
    wiki_files = [str(f.relative_to(wiki_dir)) for f in wiki_dir.rglob("*") if f.is_file()]
    return CheckResult(
        name="Wiki content migrated",
        passed=False,
        message=f"wiki/ directory still exists with {len(wiki_files)} files",
        details=wiki_files[:10],
    )


def check_subtree_exists(repo_root: Path) -> CheckResult:
    """Check that the mercor-mcp-shared subtree is present."""
    subtree = find_subtree_path(repo_root)

    if subtree is None:
        return CheckResult(
            name="Subtree present",
            passed=False,
            message="mercor-mcp-shared subtree not found",
        )

    # Verify it has expected structure
    expected = ["mcp_scripts", "packages", "ui_generator"]
    missing = [d for d in expected if not (subtree / d).exists()]

    if missing:
        return CheckResult(
            name="Subtree present",
            passed=False,
            message=f"Subtree exists but missing directories: {', '.join(missing)}",
        )

    return CheckResult(
        name="Subtree present",
        passed=True,
        message=f"Found at {subtree.relative_to(repo_root)}",
    )


def check_uv_sync(repo_root: Path) -> CheckResult:
    """Check that uv sync succeeds with the current configuration."""
    try:
        result = subprocess.run(
            ["uv", "sync", "--all-extras", "--dry-run"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError:
        return CheckResult(
            name="uv sync resolves",
            passed=True,
            message="uv not available (skipped)",
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            name="uv sync resolves",
            passed=False,
            message="uv sync timed out after 60s",
        )

    if result.returncode == 0:
        return CheckResult(
            name="uv sync resolves",
            passed=True,
            message="Dependencies resolve successfully",
        )

    # Extract useful error info
    stderr_lines = result.stderr.strip().splitlines()
    error_lines = [line for line in stderr_lines if "error" in line.lower()][:5]

    return CheckResult(
        name="uv sync resolves",
        passed=False,
        message="Dependency resolution failed",
        details=error_lines or stderr_lines[:5],
    )


def check_no_old_setuptools_packages(repo_root: Path) -> CheckResult:
    """Check that setuptools config doesn't reference old packages."""
    pyproject = repo_root / "pyproject.toml"
    if not pyproject.exists():
        return CheckResult(
            name="Setuptools config clean",
            passed=True,
            message="No pyproject.toml",
        )

    try:
        data = tomllib.loads(pyproject.read_text())
    except Exception:
        return CheckResult(
            name="Setuptools config clean",
            passed=True,
            message="Could not parse pyproject.toml (skipped)",
        )

    setuptools = data.get("tool", {}).get("setuptools", {})
    packages_find = setuptools.get("packages", {}).get("find", {})
    includes = packages_find.get("include", [])

    old_packages = ["ui_generator*", "mcp_auth*", "mcp_cache*", "mcp_middleware*", "mcp_testing*"]
    found = [p for p in includes if p in old_packages]

    if found:
        return CheckResult(
            name="Setuptools config clean",
            passed=False,
            message=f"Setuptools still references old packages: {found}",
        )

    return CheckResult(
        name="Setuptools config clean",
        passed=True,
        message="No old package references in setuptools config",
    )


def print_report(report: ValidationReport) -> None:
    """Print a formatted validation report."""
    print(f"\n{'=' * 60}")
    print("POST-MIGRATION VALIDATION REPORT")
    print(f"{'=' * 60}")

    for check in report.checks:
        symbol = "✅" if check.passed else "❌"
        print(f"\n{symbol} {check.name}")
        print(f"   {check.message}")
        if check.details:
            for detail in check.details:
                print(f"   - {detail}")

    print(f"\n{'=' * 60}")
    print(f"Results: {report.passed} passed, {report.failed} failed")
    print(f"{'=' * 60}")

    if report.all_passed:
        print("\n✅ Migration validated successfully.")
    else:
        print("\n❌ Migration has issues that need attention.")


def main(repo_root: Path | None = None) -> int:
    """Main entry point.

    Args:
        repo_root: Root of the repository to validate. Defaults to current directory.

    Returns:
        Exit code: 0 if all checks pass, 1 if any fail, 2 on error.
    """
    if repo_root is None:
        repo_root = Path.cwd()

    if not (repo_root / "pyproject.toml").exists():
        print(f"Error: {repo_root} does not look like a Python project (no pyproject.toml)")
        return 2

    print(f"Validating migration: {repo_root}")

    report = ValidationReport()

    # Run all checks
    report.checks.append(check_subtree_exists(repo_root))
    report.checks.append(check_dependency(repo_root))
    report.checks.append(check_old_directories(repo_root))
    report.checks.append(check_old_scripts(repo_root))
    report.checks.append(check_wrappers(repo_root))
    report.checks.append(check_imports(repo_root))
    report.checks.append(check_ci_workflows(repo_root))
    report.checks.append(check_wiki_migrated(repo_root))
    report.checks.append(check_no_old_setuptools_packages(repo_root))
    report.checks.append(check_uv_sync(repo_root))

    print_report(report)

    return 0 if report.all_passed else 1


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path.cwd(),
        help="Repository root (default: current directory)",
    )
    args = parser.parse_args()

    sys.exit(main(args.repo))
