#!/usr/bin/env python3
"""
Audit meta-tools for action/handler mismatches.

This catches issues before bugbot by checking:
1. Every action in Literal type has a handler
2. Every handler condition is in the Literal type
3. Every action has a help entry (optional)
4. Every provider method call exists in base.py

Run: python scripts/audit_meta_tools.py
"""

import re
import sys
from pathlib import Path

META_TOOLS_PATH = Path("mcp_servers/fmp_server/tools/_meta_tools.py")
PROVIDER_PATH = Path("mcp_servers/fmp_server/providers/base.py")


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
    # Find all request.action == "xxx" patterns
    actions = set(re.findall(r'request\.action\s*==\s*"([^"]+)"', func_content))
    actions.discard("help")
    return actions


def extract_help_actions(content: str, func_name: str) -> set[str]:
    """Extract action names from HelpResponse in a function."""
    # Find the function
    pattern = rf"async def {func_name}\(.*?(?=\nasync def |\nclass |\Z)"
    match = re.search(pattern, content, re.DOTALL)
    if not match:
        return set()

    func_content = match.group(0)
    # Find HelpResponse actions dict
    actions_pattern = r"actions=\{(.*?)\}\s*,?\s*\)\.model_dump\(\)"
    actions_match = re.search(actions_pattern, func_content, re.DOTALL)
    if not actions_match:
        return set()

    actions_content = actions_match.group(1)
    # Extract action keys (quoted strings followed by :)
    actions = set(re.findall(r'"([^"]+)"\s*:', actions_content))
    return actions


def extract_provider_methods(content: str) -> set[str]:
    """Extract all async method names from provider."""
    return set(re.findall(r"async def ([a-z_][a-z0-9_]*)\s*\(", content))


def extract_provider_calls(content: str, func_name: str) -> set[str]:
    """Extract provider method calls from a function."""
    pattern = rf"async def {func_name}\(.*?(?=\nasync def |\nclass |\Z)"
    match = re.search(pattern, content, re.DOTALL)
    if not match:
        return set()

    func_content = match.group(0)
    return set(re.findall(r"provider\.([a-z_][a-z0-9_]*)\s*\(", func_content))


# Meta-tool definitions: (InputClass, function_name)
META_TOOLS = [
    ("AnalystInput", "fmp_analyst"),
    ("PricesInput", "fmp_prices"),
    ("CompanyInput", "fmp_company"),
    ("FinancialsInput", "fmp_financials"),
    ("MarketInput", "fmp_market"),
    ("AssetsInput", "fmp_assets"),
    ("NewsInput", "fmp_news"),
    ("TechnicalInput", "fmp_technical"),
]


def main():
    if not META_TOOLS_PATH.exists():
        print(f"❌ File not found: {META_TOOLS_PATH}")
        sys.exit(1)

    meta_content = META_TOOLS_PATH.read_text()
    provider_content = PROVIDER_PATH.read_text() if PROVIDER_PATH.exists() else ""
    provider_methods = extract_provider_methods(provider_content)

    print("=" * 70)
    print("META-TOOLS ACTION/HANDLER AUDIT")
    print("=" * 70)

    total_issues = 0

    for input_class, func_name in META_TOOLS:
        print(f"\n{'=' * 50}")
        print(f"{func_name} ({input_class})")
        print("=" * 50)

        literal_actions = extract_literal_actions(meta_content, input_class)
        handler_actions = extract_handler_actions(meta_content, func_name)
        help_actions = extract_help_actions(meta_content, func_name)
        provider_calls = extract_provider_calls(meta_content, func_name)

        print(f"  Literal actions: {len(literal_actions)}")
        print(f"  Handler actions: {len(handler_actions)}")
        print(f"  Help actions: {len(help_actions)}")
        print(f"  Provider calls: {len(provider_calls)}")

        issues = []

        # Check: actions in Literal but no handler
        missing_handlers = literal_actions - handler_actions
        if missing_handlers:
            issues.append(f"❌ Actions in Literal but NO handler: {sorted(missing_handlers)}")

        # Check: handlers not in Literal (unreachable code)
        unreachable = handler_actions - literal_actions
        if unreachable:
            issues.append(f"❌ Handlers NOT in Literal (unreachable): {sorted(unreachable)}")

        # Check: actions missing from help (warning only)
        if help_actions:
            missing_help = literal_actions - help_actions
            if missing_help:
                issues.append(f"⚠️  Actions missing from help: {sorted(missing_help)}")

        # Check: provider calls that don't exist
        if provider_methods:
            missing_provider = provider_calls - provider_methods
            if missing_provider:
                issues.append(f"❌ Provider methods don't exist: {sorted(missing_provider)}")

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
