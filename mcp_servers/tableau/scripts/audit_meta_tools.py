#!/usr/bin/env python3
"""
Audit meta-tools for action/handler mismatches.

This catches issues before bugbot by checking:
1. Every action in Literal type has a handler
2. Every handler condition is in the Literal type
3. Every action has a help entry
4. Delegated functions exist and are properly imported

Run: python scripts/audit_meta_tools.py
"""

import re
import sys
from pathlib import Path

META_TOOLS_PATH = Path("mcp_servers/tableau/tools/_meta_tools.py")


def extract_literal_actions(content: str, input_class: str) -> set[str]:
    """Extract action names from a Literal type in an Input class."""
    # Find the class definition and its Literal actions
    pattern = rf"class {input_class}\(BaseModel\):.*?action:\s*Literal\[(.*?)\]\s*="
    match = re.search(pattern, content, re.DOTALL)
    if not match:
        return set()

    literal_content = match.group(1)
    # Extract quoted strings
    actions = set(re.findall(r'"([^"]+)"', literal_content))
    actions.discard("help")  # help is always handled separately
    return actions


def extract_handler_actions(content: str, func_name: str) -> set[str]:
    """Extract action names from handler conditions in a function."""
    # Find the function
    pattern = rf"async def {func_name}\(.*?(?=\nasync def |\nclass |\Z)"
    match = re.search(pattern, content, re.DOTALL)
    if not match:
        return set()

    func_content = match.group(0)
    # Find all case "xxx": patterns (match/case syntax)
    actions = set(re.findall(r'case\s*"([^"]+)":', func_content))
    # Also check for if request.action == "xxx" patterns (if/elif syntax)
    actions.update(re.findall(r'request\.action\s*==\s*"([^"]+)"', func_content))
    actions.discard("help")
    return actions


def extract_help_actions(content: str, help_var: str) -> set[str]:
    """Extract action names from a HELP constant."""
    # Find the help constant
    pattern = rf"{help_var}\s*=\s*HelpResponse\(.*?actions\s*=\s*\{{(.*?)\}}\s*,?\s*\)"
    match = re.search(pattern, content, re.DOTALL)
    if not match:
        return set()

    actions_content = match.group(1)
    # Extract action keys (quoted strings followed by :)
    actions = set(re.findall(r'"([^"]+)"\s*:', actions_content))
    return actions


def extract_delegated_functions(content: str, func_name: str) -> set[str]:
    """Extract function calls that are delegated to."""
    pattern = rf"async def {func_name}\(.*?(?=\nasync def |\nclass |\Z)"
    match = re.search(pattern, content, re.DOTALL)
    if not match:
        return set()

    func_content = match.group(0)
    # Find all await function_name(...) patterns (excluding await request.xxx)
    functions = set(re.findall(r"await\s+([a-z_][a-z0-9_]*)\s*\(", func_content))
    return functions


def extract_imported_functions(content: str) -> set[str]:
    """Extract all imported function names."""
    imports = set()
    # Match multi-line imports: from tools.xxx import (\n    func1,\n    func2,\n)
    for match in re.finditer(r"from tools\.\w+ import \((.*?)\)", content, re.DOTALL):
        import_block = match.group(1)
        imports.update(re.findall(r"([a-z_][a-z0-9_]*)", import_block))
    # Match single-line imports: from tools.xxx import func
    for match in re.finditer(
        r"from tools\.\w+ import ([a-z_][a-z0-9_]*)\s*$", content, re.MULTILINE
    ):
        imports.add(match.group(1))
    return imports


# Meta-tool definitions: (InputClass, function_name, help_var)
META_TOOLS = [
    ("AdminInput", "tableau_admin", "ADMIN_HELP"),
    ("UsersInput", "tableau_users", "USERS_HELP"),
    ("ProjectsInput", "tableau_projects", "PROJECTS_HELP"),
    ("WorkbooksInput", "tableau_workbooks", "WORKBOOKS_HELP"),
    ("ViewsInput", "tableau_views", "VIEWS_HELP"),
    ("DatasourcesInput", "tableau_datasources", "DATASOURCES_HELP"),
    ("GroupsInput", "tableau_groups", "GROUPS_HELP"),
]


def main():
    if not META_TOOLS_PATH.exists():
        print(f"❌ File not found: {META_TOOLS_PATH}")
        sys.exit(1)

    content = META_TOOLS_PATH.read_text()
    imported_functions = extract_imported_functions(content)

    print("=" * 70)
    print("META-TOOLS ACTION/HANDLER AUDIT")
    print("=" * 70)

    total_issues = 0

    for input_class, func_name, help_var in META_TOOLS:
        print(f"\n{'=' * 50}")
        print(f"{func_name} ({input_class})")
        print("=" * 50)

        literal_actions = extract_literal_actions(content, input_class)
        handler_actions = extract_handler_actions(content, func_name)
        help_actions = extract_help_actions(content, help_var)
        delegated = extract_delegated_functions(content, func_name)

        print(f"  Literal actions: {len(literal_actions)}")
        print(f"  Handler actions: {len(handler_actions)}")
        print(f"  Help actions: {len(help_actions)}")
        print(f"  Delegated functions: {len(delegated)}")

        issues = []

        # Check: actions in Literal but no handler
        missing_handlers = literal_actions - handler_actions
        if missing_handlers:
            issues.append(f"❌ Actions in Literal but NO handler: {sorted(missing_handlers)}")

        # Check: handlers not in Literal (unreachable code)
        unreachable = handler_actions - literal_actions
        if unreachable:
            issues.append(f"❌ Handlers NOT in Literal (unreachable): {sorted(unreachable)}")

        # Check: actions missing from help
        missing_help = literal_actions - help_actions
        if missing_help:
            issues.append(f"⚠️  Actions missing from help: {sorted(missing_help)}")

        # Check: delegated functions not imported
        missing_imports = (
            delegated - imported_functions - {"model_dump"}
        )  # exclude internal methods
        if missing_imports:
            issues.append(f"❌ Delegated functions not imported: {sorted(missing_imports)}")

        if issues:
            for issue in issues:
                print(f"  {issue}")
                total_issues += 1 if issue.startswith("❌") else 0
        else:
            print("  ✅ All checks passed")

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)

    if total_issues > 0:
        print(f"\n❌ Found {total_issues} critical issues!")
        sys.exit(1)
    else:
        print("\n✅ All meta-tools verified - no issues found!")
        sys.exit(0)


if __name__ == "__main__":
    main()
