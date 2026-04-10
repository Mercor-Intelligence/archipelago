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
            # Skip __pycache__, test files, and REST bridge wrappers (*_api.py)
            if "__pycache__" in str(py_file) or py_file.name.startswith("test_"):
                continue
            if py_file.name.endswith("_api.py"):
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

                for importer, modname, ispkg in pkgutil.iter_modules(module.__path__):
                    # Skip packages, private modules, and REST bridge wrappers (*_api)
                    if not ispkg and not modname.startswith("_") and not modname.endswith("_api"):
                        submodule_path = f"{module_path}.{modname}"
                        try:
                            submodule = importlib.import_module(submodule_path)
                            submodule_tools = self._extract_tools_from_module(submodule)
                            if submodule_tools:
                                print(f"    Found {len(submodule_tools)} tool(s) in {modname}")
                            tools.extend(submodule_tools)
                        except Exception as e:
                            print(f"    Warning: Failed to import {submodule_path}: {e}")
            else:
                # It's a single module
                tools = self._extract_tools_from_module(module)

            return tools
        except ImportError as e:
            raise ImportError(f"Failed to import module {module_path}: {e}")

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

        for name, obj in inspect.getmembers(module):
            if inspect.isfunction(obj) or inspect.iscoroutinefunction(obj):
                tool_info = self._extract_tool_info(obj)
                if tool_info:
                    tools.append(tool_info)

        return tools

    def _extract_tool_info(self, func: Callable) -> dict[str, Any] | None:
        """Extract metadata from a tool function."""
        from typing import Annotated, get_args, get_origin, get_type_hints

        from pydantic import create_model
        from pydantic.fields import FieldInfo

        # Get type hints (resolves forward references from __future__ annotations)
        try:
            type_hints = get_type_hints(func, include_extras=True)
        except (ValueError, TypeError, NameError):
            type_hints = {}

        try:
            sig = inspect.signature(func)
        except (ValueError, TypeError):
            return None

        # Check if function has parameters
        params = list(sig.parameters.values())

        # Filter out 'self' and 'cls' parameters
        tool_params = [p for p in params if p.name not in ("self", "cls")]

        # Get output model from return type hint
        output_model = None
        if sig.return_annotation != inspect.Signature.empty:
            output_model = type_hints.get("return", sig.return_annotation)
            if isinstance(output_model, str):
                output_model = sig.return_annotation

        # Get description from docstring
        description = inspect.getdoc(func) or ""

        is_output_pydantic = (
            output_model and inspect.isclass(output_model) and issubclass(output_model, BaseModel)
        )

        # Case 1: Single Pydantic model parameter (original behavior)
        if len(tool_params) == 1:
            input_param = tool_params[0]
            if input_param.annotation == inspect.Parameter.empty:
                return None

            input_model = type_hints.get(input_param.name, input_param.annotation)
            if isinstance(input_model, str):
                input_model = input_param.annotation

            if inspect.isclass(input_model) and issubclass(input_model, BaseModel):
                return {
                    "name": func.__name__,
                    "function": func,
                    "description": description,
                    "input_model": input_model,
                    "output_model": output_model if is_output_pydantic else None,
                    "is_async": inspect.iscoroutinefunction(func),
                    "method": "POST",
                }

        # Case 2: Multiple Annotated[type, Field(...)] parameters or zero params
        # Require a Pydantic output model to distinguish tools from helper functions
        if not is_output_pydantic:
            return None

        if len(tool_params) == 0:
            # Parameterless tool — only include if it looks like a real tool
            # (has a docstring and isn't a simple getter/factory)
            if not description or func.__name__.startswith("get_"):
                return None
            input_model = create_model(f"{func.__name__}_Input")
            return {
                "name": func.__name__,
                "function": func,
                "description": description,
                "input_model": input_model,
                "output_model": output_model,
                "is_async": inspect.iscoroutinefunction(func),
                "method": "POST",
            }

        # Try to build a dynamic model from Annotated params
        model_fields: dict[str, Any] = {}
        has_annotated_field = False
        for param in tool_params:
            hint = type_hints.get(param.name)
            if hint is None:
                return None

            has_default = param.default != inspect.Parameter.empty

            # Extract type and FieldInfo from Annotated[type, Field(...)]
            if get_origin(hint) is Annotated:
                has_annotated_field = True
                args = get_args(hint)
                base_type = args[0]
                field_info = next((a for a in args[1:] if isinstance(a, FieldInfo)), None)
                if field_info is not None:
                    # Merge function default into FieldInfo if the Field doesn't have one
                    if has_default and not field_info.is_required():
                        # FieldInfo already has a default set — use as-is
                        model_fields[param.name] = (base_type, field_info)
                    elif has_default:
                        # FieldInfo has no default but the function param does —
                        # rebuild FieldInfo with the default from the function signature
                        from pydantic import Field as PydanticField

                        # Copy metadata from original FieldInfo
                        kwargs: dict[str, Any] = {"default": param.default}
                        if field_info.description:
                            kwargs["description"] = field_info.description
                        for constraint in field_info.metadata:
                            for attr in (
                                "min_length",
                                "max_length",
                                "gt",
                                "ge",
                                "lt",
                                "le",
                                "pattern",
                            ):
                                val = getattr(constraint, attr, None)
                                if val is not None:
                                    kwargs[attr] = val
                        model_fields[param.name] = (base_type, PydanticField(**kwargs))
                    else:
                        model_fields[param.name] = (base_type, field_info)
                elif has_default:
                    model_fields[param.name] = (base_type, param.default)
                else:
                    model_fields[param.name] = (base_type, ...)
            elif inspect.isclass(hint) and issubclass(hint, BaseModel):
                return None
            else:
                # Plain typed parameter
                if has_default:
                    model_fields[param.name] = (hint, param.default)
                else:
                    model_fields[param.name] = (hint, ...)

        # Require at least one Annotated field to distinguish tools from random functions
        if not has_annotated_field:
            return None

        if not model_fields:
            return None

        try:
            input_model = create_model(f"{func.__name__}_Input", **model_fields)
        except Exception:
            return None

        return {
            "name": func.__name__,
            "function": func,
            "description": description,
            "input_model": input_model,
            "output_model": output_model if is_output_pydantic else None,
            "is_async": inspect.iscoroutinefunction(func),
            "method": "POST",
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
