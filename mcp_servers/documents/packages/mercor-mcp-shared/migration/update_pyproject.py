#!/usr/bin/env python
"""Update pyproject.toml to add mercor-mcp-shared dependency.

This script uses `uv add` to add the mercor-mcp-shared path dependency (subtree).
It is idempotent - running it multiple times will not duplicate entries.

It performs the following cleanup operations:
- Removes conflicting local path sources for packages provided by mercor-mcp-shared
  (mcp-testing, mcp-middleware, mcp-auth, mcp-cache)
- Removes [project.scripts] entries that reference ui_generator (now provided by shared)
- Cleans up [tool.setuptools.*] sections that reference ui_generator
- Sets [tool.setuptools] packages = [] to prevent auto-discovery conflicts
"""

import re
import subprocess
import sys
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # Fallback for Python < 3.11


DEPENDENCY_NAME = "mercor-mcp-shared"
SUBTREE_PATH = "packages/mercor-mcp-shared"

# Packages provided by mercor-mcp-shared that may have conflicting local sources
CONFLICTING_PACKAGES = ["mcp-testing", "mcp-middleware", "mcp-auth", "mcp-cache"]

# Scripts that reference ui_generator (now provided by mercor-mcp-shared)
UI_GENERATOR_SCRIPTS = ["mcp-ui-gen", "mcp-ui"]


def check_pydantic_models(repo_root: Path) -> dict:
    """Check for Pydantic models and their base classes.

    Scans Python files for classes that inherit from BaseModel or GeminiBaseModel.
    GeminiBaseModel provides Gemini-compatible JSON schemas which work better with
    the UI generator.

    Returns a dict with:
    - base_model_files: list of (file, class_name) using plain BaseModel
    - gemini_model_files: list of (file, class_name) using GeminiBaseModel
    """
    import ast

    base_model_classes = []
    gemini_model_classes = []

    # Directories to skip
    skip_dirs = {".git", ".venv", "node_modules", "__pycache__", ".ruff_cache", "build", "dist"}

    for py_file in repo_root.rglob("*.py"):
        # Skip files in excluded directories
        if any(part in skip_dirs for part in py_file.parts):
            continue

        try:
            content = py_file.read_text()
            tree = ast.parse(content)
        except Exception:
            continue

        # Check imports to understand what's being used
        has_base_model_import = False
        has_gemini_import = False
        base_model_alias = "BaseModel"
        gemini_alias = "GeminiBaseModel"

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module == "pydantic":
                    for alias in node.names:
                        if alias.name == "BaseModel":
                            has_base_model_import = True
                            base_model_alias = alias.asname or alias.name
                elif node.module == "mcp_schema" or (
                    node.module and node.module.endswith(".gemini")
                ):
                    for alias in node.names:
                        if alias.name == "GeminiBaseModel":
                            has_gemini_import = True
                            gemini_alias = alias.asname or alias.name

        # Find class definitions
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for base in node.bases:
                    base_name = None
                    if isinstance(base, ast.Name):
                        base_name = base.id
                    elif isinstance(base, ast.Attribute):
                        base_name = base.attr

                    if base_name:
                        rel_path = py_file.relative_to(repo_root)
                        if base_name == gemini_alias and has_gemini_import:
                            gemini_model_classes.append((str(rel_path), node.name))
                        elif base_name == base_model_alias and has_base_model_import:
                            base_model_classes.append((str(rel_path), node.name))

    return {
        "base_model_files": base_model_classes,
        "gemini_model_files": gemini_model_classes,
    }


def report_pydantic_models(repo_root: Path) -> None:
    """Report on Pydantic model usage and suggest GeminiBaseModel migration."""
    result = check_pydantic_models(repo_root)

    base_models = result["base_model_files"]
    gemini_models = result["gemini_model_files"]

    print("\n" + "=" * 60)
    print("Pydantic Model Analysis")
    print("=" * 60)

    if gemini_models:
        print(f"\n✅ Found {len(gemini_models)} class(es) using GeminiBaseModel:")
        for file, class_name in gemini_models[:10]:
            print(f"   {file}: {class_name}")
        if len(gemini_models) > 10:
            print(f"   ... and {len(gemini_models) - 10} more")

    if base_models:
        print(f"\n⚠️  Found {len(base_models)} class(es) using plain BaseModel:")
        for file, class_name in base_models[:10]:
            print(f"   {file}: {class_name}")
        if len(base_models) > 10:
            print(f"   ... and {len(base_models) - 10} more")

        print("\n💡 Suggestion: Consider migrating to GeminiBaseModel for better")
        print("   UI generator compatibility. GeminiBaseModel provides:")
        print("   - Gemini-compatible JSON schemas (no unsupported keywords)")
        print("   - Automatic title/description handling")
        print("   - Better integration with mcp-ui-gen")
        print("\n   To migrate, change:")
        print("     from pydantic import BaseModel")
        print("   to:")
        print("     from mcp_schema import GeminiBaseModel")
        print("\n   Then update class definitions:")
        print("     class MyModel(GeminiBaseModel):")

    if not base_models and not gemini_models:
        print("\nNo Pydantic model classes found.")

    print()


def remove_conflicting_sources(filepath: Path, dry_run: bool = False) -> list[str]:
    """Remove conflicting local path sources from pyproject.toml.

    Returns list of packages that were removed.
    """
    content = filepath.read_text()
    removed = []

    for pkg in CONFLICTING_PACKAGES:
        # Match lines like: mcp-testing = { path = "..." }
        # or: mcp-testing = {path = "..."}
        pattern = rf'^{re.escape(pkg)}\s*=\s*\{{[^}}]*path\s*=\s*["\'][^"\']*["\'][^}}]*\}}\s*\n?'
        if re.search(pattern, content, re.MULTILINE):
            if not dry_run:
                content = re.sub(pattern, "", content, flags=re.MULTILINE)
            removed.append(pkg)

    if removed and not dry_run:
        filepath.write_text(content)

    return removed


def remove_conflicting_dependencies(filepath: Path, dry_run: bool = False) -> list[str]:
    """Remove conflicting packages from [project.dependencies].

    Returns list of packages that were removed.
    """
    content = filepath.read_text()
    removed = []

    for pkg in CONFLICTING_PACKAGES:
        # Match lines like: "mcp-testing", or "mcp-testing>=1.0",
        # Handle with or without trailing comma, with various spacing
        pattern = rf'^\s*"{re.escape(pkg)}[^"]*"\s*,?\s*(?:#[^\n]*)?\n?'
        if re.search(pattern, content, re.MULTILINE):
            if not dry_run:
                content = re.sub(pattern, "", content, flags=re.MULTILINE)
            removed.append(pkg)

    if removed and not dry_run:
        filepath.write_text(content)

    return removed


def remove_ui_generator_scripts(filepath: Path, dry_run: bool = False) -> list[str]:
    """Remove [project.scripts] entries that reference ui_generator.

    After migration, ui_generator is provided by mercor-mcp-shared,
    so local script entries like mcp-ui-gen and mcp-ui should be removed.

    Returns list of script names that were removed.
    """
    content = filepath.read_text()
    removed = []

    for script in UI_GENERATOR_SCRIPTS:
        # Match lines like: mcp-ui-gen = "ui_generator.cli.main:cli"
        pattern = rf'^\s*{re.escape(script)}\s*=\s*"ui_generator[^"]*"\s*\n?'
        if re.search(pattern, content, re.MULTILINE):
            if not dry_run:
                content = re.sub(pattern, "", content, flags=re.MULTILINE)
            removed.append(script)

    # Clean up empty [project.scripts] section if all scripts were removed
    if removed and not dry_run:
        # Remove empty [project.scripts] section
        content = re.sub(r"\[project\.scripts\]\s*\n(?=\[|\Z)", "", content)
        filepath.write_text(content)

    return removed


def remove_ui_generator_setuptools_config(filepath: Path, dry_run: bool = False) -> list[str]:
    """Remove [tool.setuptools.*] entries that reference ui_generator.

    This handles:
    - [tool.setuptools.packages.find] - removes ui_generator from include, removes section if empty
    - [tool.setuptools.package-data] - removes ui_generator entry

    Uses proper TOML parsing to determine what to modify, then applies targeted
    regex replacements to preserve formatting.

    Returns list of sections/entries that were modified.
    """
    content = filepath.read_text()
    modified = []

    # Parse TOML to accurately detect sections that need modification
    try:
        toml_data = tomllib.loads(content)
    except Exception:
        # If TOML parsing fails, skip this step
        return modified

    # Check [tool.setuptools.packages.find].include for ui_generator entries
    packages_find = (
        toml_data.get("tool", {}).get("setuptools", {}).get("packages", {}).get("find", {})
    )
    include_list = packages_find.get("include", [])

    ui_generator_entries = [entry for entry in include_list if entry.startswith("ui_generator")]
    if ui_generator_entries:
        remaining_entries = [
            entry for entry in include_list if not entry.startswith("ui_generator")
        ]

        if not remaining_entries:
            # Include list will be empty - remove entire [tool.setuptools.packages.find] section
            packages_find_pattern = r"\[tool\.setuptools\.packages\.find\].*?(?=\n\[|\Z)"
            if not dry_run:
                content = re.sub(packages_find_pattern, "", content, flags=re.DOTALL)
                content = re.sub(r"\n{3,}", "\n\n", content)
            modified.append("[tool.setuptools.packages.find] (removed)")
        else:
            # Remove only ui_generator entries from include list
            # Target the include line specifically within [tool.setuptools.packages.find] section
            section_pattern = (
                r"(\[tool\.setuptools\.packages\.find\].*?)(include\s*=\s*\[)([^\]]*)(])"
            )
            match = re.search(section_pattern, content, flags=re.DOTALL)
            if match:
                include_content = match.group(3)
                # Remove ui_generator entries
                new_include = re.sub(r'"ui_generator[^"]*"\s*,?\s*', "", include_content)
                new_include = new_include.strip()
                new_include = re.sub(r"^,\s*", "", new_include)
                new_include = re.sub(r",\s*$", "", new_include)
                if not dry_run:
                    content = content[: match.start(3)] + new_include + content[match.end(3) :]
                modified.append("[tool.setuptools.packages.find] include")

    # Check [tool.setuptools.package-data] for ui_generator entry
    package_data = toml_data.get("tool", {}).get("setuptools", {}).get("package-data", {})
    if "ui_generator" in package_data:
        # Remove ui_generator entry from [tool.setuptools.package-data]
        package_data_pattern = r'^\s*"ui_generator"\s*=\s*\[[^\]]*\]\s*\n?'
        if not dry_run:
            content = re.sub(package_data_pattern, "", content, flags=re.MULTILINE)
        modified.append("[tool.setuptools.package-data] ui_generator")

    # Clean up empty [tool.setuptools.package-data] section
    if modified and not dry_run:
        content = re.sub(r"\[tool\.setuptools\.package-data\]\s*\n(?=\[|\Z)", "", content)
        # Clean up any trailing whitespace
        content = content.rstrip() + "\n"
        filepath.write_text(content)

    return modified


def update_setuptools_config(filepath: Path, dry_run: bool = False) -> bool:
    """Update [tool.setuptools] to prevent auto-discovery conflicts.

    After migration, the repo no longer has its own packages to distribute.
    This ensures setuptools doesn't try to auto-discover packages like
    'ui', 'mcp_servers', etc.

    Returns True if changes were made.
    """
    content = filepath.read_text()

    # If [tool.setuptools.packages.find] exists, we cannot add packages = []
    # because they conflict in TOML (packages can't be both an array and a table)
    if "[tool.setuptools.packages.find]" in content:
        return False

    # Check if [tool.setuptools] already exists
    if "[tool.setuptools]" in content:
        # Check if it already has packages = []
        if re.search(r"\[tool\.setuptools\]\s*\n\s*packages\s*=\s*\[\s*\]", content):
            return False

        # Check if it has a packages list that references ui_generator or other local packages
        # Pattern: [tool.setuptools]\npackages = ["ui_generator", ...]
        packages_pattern = r"(\[tool\.setuptools\]\s*\n\s*packages\s*=\s*)\[[^\]]*\]"
        match = re.search(packages_pattern, content)
        if match:
            # Replace the packages list with empty list
            if not dry_run:
                content = re.sub(
                    packages_pattern,
                    r"\1[]  # No local packages - all shared code from mercor-mcp-shared",
                    content,
                )
                filepath.write_text(content)
            return True

        # Has [tool.setuptools] but no packages key - add packages = []
        setuptools_pattern = r"(\[tool\.setuptools\]\s*\n)"
        if re.search(setuptools_pattern, content):
            if not dry_run:
                content = re.sub(
                    setuptools_pattern,
                    r"\1packages = []  # No local packages - all shared code from "
                    r"mercor-mcp-shared\n",
                    content,
                )
                filepath.write_text(content)
            return True

        return False

    # Add [tool.setuptools] with packages = []
    # Insert after [build-system] section
    # Use a pattern that matches until the next section header (line starting with [)
    # but not array brackets within values
    build_system_pattern = r'(\[build-system\].*?)(?=\n\[(?!\s*")|\Z)'
    match = re.search(build_system_pattern, content, re.DOTALL)
    if match:
        build_system_section = match.group(1)
        new_section = (
            build_system_section.rstrip() + "\n\n[tool.setuptools]\npackages = []  "
            "# No local packages - all shared code from mercor-mcp-shared\n"
        )
        if not dry_run:
            content = content.replace(build_system_section, new_section)
            filepath.write_text(content)
        return True

    return False


def add_dependency_with_uv(repo_root: Path, dry_run: bool = False) -> dict:
    """Add dependency using uv add command.

    This handles all the pyproject.toml formatting correctly:
    - Adds the dependency to [project.dependencies]
    - Adds the path source to [tool.uv.sources]
    """
    # Use path to the subtree
    command = ["uv", "add", DEPENDENCY_NAME, "--path", SUBTREE_PATH]

    if dry_run:
        return {
            "status": "would_update",
            "command": " ".join(command),
        }

    try:
        result = subprocess.run(
            command,
            cwd=repo_root,
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            # Check if it failed because already exists
            if "already" in result.stderr.lower() or "already" in result.stdout.lower():
                return {"status": "unchanged", "message": "Already configured"}
            return {
                "status": "error",
                "message": f"uv add failed: {result.stderr or result.stdout}",
            }

        return {"status": "updated", "changes": ["Added dependency with path source"]}

    except FileNotFoundError:
        return {"status": "error", "message": "uv command not found. Please install uv."}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def update_pyproject(filepath: Path, dry_run: bool = False) -> dict:
    """Update pyproject.toml with mercor-mcp-shared dependency.

    Returns a dict with results. Uses `uv add` which handles idempotency.
    """
    repo_root = filepath.parent

    if not filepath.exists():
        return {"status": "error", "message": "pyproject.toml not found"}

    changes = []

    # First, remove conflicting local path sources
    removed_sources = remove_conflicting_sources(filepath, dry_run)
    if removed_sources:
        changes.append(f"Removed conflicting sources: {', '.join(removed_sources)}")

    # Remove conflicting dependencies from [project.dependencies]
    removed_deps = remove_conflicting_dependencies(filepath, dry_run)
    if removed_deps:
        changes.append(f"Removed conflicting dependencies: {', '.join(removed_deps)}")

    # Remove [project.scripts] entries referencing ui_generator
    removed_scripts = remove_ui_generator_scripts(filepath, dry_run)
    if removed_scripts:
        changes.append(f"Removed ui_generator scripts: {', '.join(removed_scripts)}")

    # Remove ui_generator from [tool.setuptools.*] sections
    removed_setuptools = remove_ui_generator_setuptools_config(filepath, dry_run)
    if removed_setuptools:
        changes.append(f"Cleaned up setuptools config: {', '.join(removed_setuptools)}")

    # Update setuptools config to prevent auto-discovery conflicts
    if update_setuptools_config(filepath, dry_run):
        changes.append("Set [tool.setuptools] packages = [] to prevent auto-discovery")

    # Now add mercor-mcp-shared
    result = add_dependency_with_uv(repo_root, dry_run)

    if result["status"] == "error":
        return result

    if result["status"] == "updated":
        changes.extend(result.get("changes", []))

    if changes:
        return {"status": "updated", "changes": changes}

    return result


def main(
    repo_root: Path | None = None,
    dry_run: bool = False,
    check_models: bool = True,
) -> int:
    """Main entry point.

    Args:
        repo_root: Root of the repository to update. Defaults to current directory.
        dry_run: If True, don't write changes.
        check_models: If True, analyze Pydantic models and suggest GeminiBaseModel migration.

    Returns:
        Exit code (0 for success).
    """
    if repo_root is None:
        repo_root = Path.cwd()

    pyproject_path = repo_root / "pyproject.toml"

    print(f"Repository: {repo_root}")
    print(f"pyproject.toml: {pyproject_path}")
    print(f"Subtree path: {SUBTREE_PATH}")
    print(f"Dry run: {dry_run}")
    print()

    result = update_pyproject(pyproject_path, dry_run)

    if result["status"] == "error":
        print(f"Error: {result['message']}")
        return 1

    if result["status"] == "unchanged":
        print(f"✅ {result['message']}")
    elif result["status"] == "would_update":
        print("Would run:")
        print(f"  {result['command']}")
        print()
        print("(Dry run - no changes made)")
    elif result["status"] == "updated":
        print("Made the following changes:")
        for change in result["changes"]:
            print(f"  - {change}")

    # Check for Pydantic models and suggest GeminiBaseModel migration
    if check_models:
        report_pydantic_models(repo_root)

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
    parser.add_argument(
        "--no-check-models",
        action="store_true",
        help="Skip Pydantic model analysis",
    )
    args = parser.parse_args()

    sys.exit(main(args.repo, args.dry_run, check_models=not args.no_check_models))
