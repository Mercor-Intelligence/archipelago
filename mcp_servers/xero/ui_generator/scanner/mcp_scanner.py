"""
MCP Tool Scanner - Find and extract tool definitions from MCP server codebases.
"""

import ast
import importlib
import importlib.util
import inspect
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel


class MCPToolScanner:
    """Scan directories and modules for MCP tool definitions."""

    def scan_directory(self, path: str | Path) -> list[dict[str, Any]]:
        """
        Scan directory for MCP tool definitions.

        Args:
            path: Directory path to scan

        Returns:
            List of tool metadata dictionaries
        """
        path = Path(path)
        tools = []

        for py_file in path.rglob("*.py"):
            # Skip __pycache__ and test files
            if "__pycache__" in str(py_file) or py_file.name.startswith("test_"):
                continue

            try:
                file_tools = self._scan_file(py_file)
                tools.extend(file_tools)
            except Exception as e:
                print(f"Warning: Failed to scan {py_file}: {e}")

        return tools

    def scan_module(self, module_path: str) -> list[dict[str, Any]]:
        """
        Scan a Python module or package for tool definitions.

        Args:
            module_path: Module path (e.g., 'tools.quickbooks' or 'tools')

        Returns:
            List of tool metadata dictionaries
        """
        try:
            module = importlib.import_module(module_path)
            tools = []

            # Check if this is a package (has __path__)
            if hasattr(module, "__path__"):
                # It's a package - scan all submodules
                import pkgutil

                for _importer, modname, ispkg in pkgutil.iter_modules(module.__path__):
                    if not ispkg and not modname.startswith("_"):
                        submodule_path = f"{module_path}.{modname}"
                        try:
                            submodule = importlib.import_module(submodule_path)
                            tools.extend(self._extract_tools_from_module(submodule))
                        except Exception as e:
                            print(f"Warning: Failed to scan {submodule_path}: {e}")
            else:
                # It's a single module
                tools = self._extract_tools_from_module(module)

            return tools
        except ImportError as e:
            raise ImportError(f"Failed to import module {module_path}: {e}") from e

    def _scan_file(self, filepath: Path) -> list[dict[str, Any]]:
        """Scan a single Python file for tools using AST parsing."""
        tools = []

        try:
            with open(filepath) as f:
                source = f.read()

            tree = ast.parse(source)

            # Look for function definitions
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef):
                    # Check if it's decorated (potential tool)
                    if node.decorator_list:
                        tool_info = self._extract_tool_from_ast(node, filepath)
                        if tool_info:
                            tools.append(tool_info)
        except SyntaxError:
            # If AST parsing fails, try dynamic import
            tools = self._scan_file_dynamic(filepath)

        return tools

    def _scan_file_dynamic(self, filepath: Path) -> list[dict[str, Any]]:
        """Dynamically import and scan a file."""
        try:
            spec = importlib.util.spec_from_file_location("temp_module", filepath)
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                sys.modules["temp_module"] = module
                spec.loader.exec_module(module)

                tools = self._extract_tools_from_module(module)

                # Clean up
                if "temp_module" in sys.modules:
                    del sys.modules["temp_module"]

                return tools
        except Exception as e:
            print(f"Warning: Dynamic import failed for {filepath}: {e}")

        return []

    def _extract_tools_from_module(self, module) -> list[dict[str, Any]]:
        """Extract tool functions from a loaded module."""
        tools = []

        for _name, obj in inspect.getmembers(module):
            if inspect.isfunction(obj) or inspect.iscoroutinefunction(obj):
                tool_info = self._extract_tool_info(obj)
                if tool_info:
                    tools.append(tool_info)

        return tools

    def _extract_tool_info(self, func: Callable) -> dict[str, Any] | None:
        """Extract metadata from a tool function."""
        # Get type hints
        try:
            sig = inspect.signature(func)
        except (ValueError, TypeError):
            return None

        # Check if function has parameters
        params = list(sig.parameters.values())

        # Filter out 'self' and 'cls' parameters
        tool_params = [p for p in params if p.name not in ("self", "cls")]

        # MCP tools must have EXACTLY 1 parameter (the Pydantic input model)
        if len(tool_params) != 1:
            return None

        input_param = tool_params[0]

        if input_param.annotation == inspect.Parameter.empty:
            return None

        input_model = input_param.annotation

        # Get output model from return type hint
        output_model = None
        if sig.return_annotation != inspect.Signature.empty:
            output_model = sig.return_annotation

        # Get description from docstring
        description = inspect.getdoc(func) or ""

        # Check if input/output are Pydantic models
        is_input_pydantic = inspect.isclass(input_model) and issubclass(input_model, BaseModel)
        is_output_pydantic = (
            output_model and inspect.isclass(output_model) and issubclass(output_model, BaseModel)
        )

        # Only include if input is a Pydantic model
        if not is_input_pydantic:
            return None

        return {
            "name": func.__name__,
            "function": func,
            "description": description,
            "input_model": input_model,
            "output_model": output_model if is_output_pydantic else None,
            "is_async": inspect.iscoroutinefunction(func),
            "method": "POST",  # Default to POST for MCP tools
        }

    def _extract_tool_from_ast(
        self, node: ast.FunctionDef, filepath: Path
    ) -> dict[str, Any] | None:
        """Extract tool info from AST node (static analysis)."""
        # Get function name
        name = node.name

        # Get docstring
        description = ast.get_docstring(node) or ""

        # Try to extract type annotations from AST
        # This is limited but better than nothing
        has_type_hints = False
        if node.args.args:
            for arg in node.args.args:
                if arg.annotation:
                    has_type_hints = True
                    break

        if not has_type_hints:
            return None

        # For AST-based extraction, we return minimal info
        # The caller may need to do dynamic import for full details
        return {
            "name": name,
            "description": description,
            "filepath": str(filepath),
            "needs_dynamic_import": True,
        }
