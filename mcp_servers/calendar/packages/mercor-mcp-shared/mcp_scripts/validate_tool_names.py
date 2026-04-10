#!/usr/bin/env python3
"""Validate that all MCP tool names use lowercase + underscores only.

This script scans mcp_servers/ directories for mcp.tool() registrations
and validates that all tool names follow the naming convention:
lowercase letters and underscores only.

Valid:   lookup_location, search_companies, get_investor_investments
Invalid: dealroom.GetSimilarCompanies, getCompany, search-companies
"""

import ast
import re
import sys
from pathlib import Path

from mcp_scripts.logging_config import get_logger

logger = get_logger(__name__)

# Pattern for valid tool names: starts with lowercase letter, then lowercase/underscores
VALID_PATTERN = re.compile(r"^[a-z][a-z_]*$")


class ToolNameVisitor(ast.NodeVisitor):
    """AST visitor to extract MCP tool registrations and their names."""

    def __init__(self, filepath: Path):
        self.filepath = filepath
        self.violations: list[tuple[int, str]] = []
        self.processed_decorators: set[int] = set()  # Track decorator node IDs

    def _check_function_decorators(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        """Check function decorators for @mcp.tool() registrations."""
        for decorator in node.decorator_list:
            # Skip non-Call decorators (e.g., @public_tool which is ast.Name)
            if not isinstance(decorator, ast.Call):
                continue

            if self._is_mcp_tool_call(decorator):
                # Mark this decorator as processed to avoid duplicate checking
                self.processed_decorators.add(id(decorator))

                # Check for explicit name= parameter
                tool_name = self._extract_explicit_name(decorator)
                if tool_name is None:
                    # No explicit name, use function name
                    tool_name = node.name

                if not VALID_PATTERN.match(tool_name):
                    self.violations.append((node.lineno, tool_name))
                break

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        """Visit function definitions to find @mcp.tool() decorators."""
        self._check_function_decorators(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        """Visit async function definitions to find @mcp.tool() decorators."""
        self._check_function_decorators(node)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        """Visit function call nodes to find mcp.tool() registrations."""
        # Skip if this decorator was already processed
        if id(node) in self.processed_decorators:
            self.generic_visit(node)
            return

        if self._is_mcp_tool_call(node):
            tool_name = self._extract_tool_name(node)
            if tool_name is not None and not VALID_PATTERN.match(tool_name):
                self.violations.append((node.lineno, tool_name))

        self.generic_visit(node)

    def _is_mcp_tool_call(self, node: ast.Call) -> bool:
        """Check if a Call node is an mcp.tool() registration."""
        # Check for: mcp.tool(...)
        if isinstance(node.func, ast.Attribute):
            return (
                node.func.attr == "tool"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in ("mcp", "server", "app")
            )
        return False

    def _extract_explicit_name(self, node: ast.Call) -> str | None:
        """Extract explicit name= parameter from mcp.tool() call."""
        for keyword in node.keywords:
            if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                value = keyword.value.value
                # Only return if it's a string constant
                if isinstance(value, str):
                    return value
        return None

    def _extract_tool_name(self, node: ast.Call) -> str | None:
        """Extract the tool name from an mcp.tool() call.

        Checks for explicit name= parameter first, then falls back to
        inferring the name from the function being registered.
        """
        # Check for explicit name= parameter: mcp.tool(func, name="...")
        tool_name = self._extract_explicit_name(node)
        if tool_name is not None:
            return tool_name

        # No explicit name=, try to infer from function being registered
        if node.args:
            first_arg = node.args[0]

            # Handle: mcp.tool(module.function)
            if isinstance(first_arg, ast.Attribute):
                return first_arg.attr

            # Handle: mcp.tool(function)
            if isinstance(first_arg, ast.Name):
                return first_arg.id

        return None


def validate_file(filepath: Path) -> list[tuple[Path, int, str]]:
    """Validate tool names in a single Python file.

    Args:
        filepath: Path to Python file to validate

    Returns:
        List of (filepath, line_number, tool_name) tuples for violations
    """
    try:
        with open(filepath, encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=str(filepath))

        visitor = ToolNameVisitor(filepath)
        visitor.visit(tree)

        return [(filepath, lineno, name) for lineno, name in visitor.violations]
    except SyntaxError:
        # Skip files with syntax errors (might be templates or partial files)
        return []
    except Exception as e:
        logger.warning("Could not parse %s: %s", filepath, e)
        return []


def validate_directory(directory: Path) -> list[tuple[Path, int, str]]:
    """Validate all Python files in a directory tree.

    Args:
        directory: Root directory to scan for Python files

    Returns:
        List of all violations found
    """
    violations = []

    for py_file in directory.rglob("*.py"):
        violations.extend(validate_file(py_file))

    return violations


def main() -> int:
    """Main entry point for the validation script.

    Returns:
        0 if all tool names are valid, 1 if violations found
    """
    # Default to scanning mcp_servers/ if it exists
    if len(sys.argv) > 1:
        scan_path = Path(sys.argv[1])
    else:
        scan_path = Path("mcp_servers")

    if not scan_path.exists():
        logger.error("Directory '%s' does not exist", scan_path)
        return 1

    if not scan_path.is_dir():
        logger.error("'%s' is not a directory. Please provide a directory path.", scan_path)
        return 1

    logger.info("Scanning %s for MCP tool name violations...", scan_path)

    violations = validate_directory(scan_path)

    if violations:
        logger.error("Invalid tool names found (must be lowercase + underscores):")

        for filepath, lineno, tool_name in violations:
            logger.error("  %s:%s -> '%s'", filepath, lineno, tool_name)

        logger.error("Pattern required: ^[a-z][a-z_]*$")
        logger.error("Found %s violation(s)", len(violations))
        logger.info("Examples of valid names:")
        logger.info("  lookup_location")
        logger.info("  search_companies")
        logger.info("  get_investor_investments")
        logger.info("Examples of invalid names:")
        logger.info("  dealroom.GetSimilarCompanies  (dots and camelCase)")
        logger.info("  getCompany  (camelCase)")
        logger.info("  search-companies  (hyphens not allowed)")

        return 1
    else:
        logger.info("All tool names are valid!")
        return 0


if __name__ == "__main__":
    sys.exit(main())
