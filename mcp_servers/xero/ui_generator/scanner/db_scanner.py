"""
Database Model Scanner - Detect and parse SQLAlchemy models from MCP servers.

Scans for db/models.py files and extracts table schemas.
"""

import ast
import importlib.util
import sys
from pathlib import Path
from typing import Any


class DatabaseScanner:
    """Scanner for SQLAlchemy database models."""

    def scan_server_directory(self, server_path: str | Path) -> list[dict[str, Any]]:
        """
        Scan an MCP server directory for database models.

        Args:
            server_path: Path to MCP server directory

        Returns:
            List of table schema dictionaries
        """
        server_path = Path(server_path)
        models_file = server_path / "db" / "models.py"

        if not models_file.exists():
            return []

        try:
            return self._parse_models_file(models_file)
        except Exception as e:
            print(f"Warning: Failed to parse {models_file}: {e}")
            return []

    def _parse_models_file(self, models_file: Path) -> list[dict[str, Any]]:
        """
        Parse a SQLAlchemy models.py file.

        Args:
            models_file: Path to models.py file

        Returns:
            List of table schema dictionaries
        """
        # Try AST parsing first (safer, doesn't execute code)
        try:
            with open(models_file) as f:
                source = f.read()
            tree = ast.parse(source)
            return self._extract_models_from_ast(tree)
        except Exception as e:
            print(f"Warning: AST parsing failed, trying dynamic import: {e}")

        # Fallback to dynamic import
        try:
            return self._extract_models_dynamic(models_file)
        except Exception as e:
            print(f"Warning: Dynamic import failed: {e}")
            return []

    def _extract_models_from_ast(self, tree: ast.AST) -> list[dict[str, Any]]:
        """Extract model definitions using AST parsing."""
        tables = []

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                # Check if class has __tablename__ attribute
                table_name = None
                columns = []

                for item in node.body:
                    # Look for __tablename__ = "..."
                    if (
                        isinstance(item, ast.Assign)
                        and len(item.targets) == 1
                        and isinstance(item.targets[0], ast.Name)
                        and item.targets[0].id == "__tablename__"
                    ):
                        if isinstance(item.value, ast.Constant):
                            table_name = item.value.value

                    # Look for column definitions
                    # Old syntax: col_name = Column(...)
                    # New syntax: col_name: Mapped[type] = mapped_column(...)
                    if isinstance(item, ast.Assign):
                        # Old SQLAlchemy 1.x syntax: id = Column(...)
                        for target in item.targets:
                            if isinstance(target, ast.Name) and isinstance(item.value, ast.Call):
                                # Check if it's a Column() call
                                if (
                                    isinstance(item.value.func, ast.Name)
                                    and item.value.func.id == "Column"
                                ):
                                    col_info = self._parse_column_from_ast(target.id, item.value)
                                    if col_info:
                                        columns.append(col_info)
                    elif isinstance(item, ast.AnnAssign):
                        # New SQLAlchemy 2.0 syntax: id: Mapped[str] = mapped_column(...)
                        if isinstance(item.target, ast.Name) and isinstance(item.value, ast.Call):
                            # Check if it's a mapped_column() call
                            if (
                                isinstance(item.value.func, ast.Name)
                                and item.value.func.id == "mapped_column"
                            ):
                                col_info = self._parse_column_from_ast(item.target.id, item.value)
                                if col_info:
                                    columns.append(col_info)

                if table_name and columns:
                    tables.append(
                        {
                            "table_name": table_name,
                            "model_name": node.name,
                            "columns": columns,
                            "docstring": ast.get_docstring(node) or "",
                        }
                    )

        return tables

    def _parse_column_from_ast(self, col_name: str, call_node: ast.Call) -> dict[str, Any] | None:
        """Parse Column() definition from AST."""
        col_type = "String"
        primary_key = False
        nullable = True
        default_value = None

        # First positional argument is usually the type
        if call_node.args:
            type_arg = call_node.args[0]
            if isinstance(type_arg, ast.Name):
                col_type = type_arg.id
            elif isinstance(type_arg, ast.Call) and isinstance(type_arg.func, ast.Name):
                col_type = type_arg.func.id

        # Check keyword arguments
        for keyword in call_node.keywords:
            if keyword.arg == "primary_key" and isinstance(keyword.value, ast.Constant):
                primary_key = keyword.value.value
            elif keyword.arg == "nullable" and isinstance(keyword.value, ast.Constant):
                nullable = keyword.value.value
            elif keyword.arg == "default" and isinstance(keyword.value, ast.Constant):
                default_value = keyword.value.value

        return {
            "name": col_name,
            "type": col_type,
            "primary_key": primary_key,
            "nullable": nullable,
            "default": default_value,
        }

    def _extract_models_dynamic(self, models_file: Path) -> list[dict[str, Any]]:
        """Extract models by dynamically importing the module."""
        # Add parent directory to path
        server_path = models_file.parent.parent
        sys.path.insert(0, str(server_path))

        try:
            # Load module dynamically
            spec = importlib.util.spec_from_file_location("db.models", models_file)
            if spec is None or spec.loader is None:
                return []

            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            # Look for SQLAlchemy Base classes
            tables = []
            for _name, obj in module.__dict__.items():
                if (
                    isinstance(obj, type)
                    and hasattr(obj, "__tablename__")
                    and hasattr(obj, "__table__")
                ):
                    table_info = self._extract_table_info_from_model(obj)
                    if table_info:
                        tables.append(table_info)

            return tables
        finally:
            # Clean up sys.path
            if str(server_path) in sys.path:
                sys.path.remove(str(server_path))

    def _extract_table_info_from_model(self, model_class) -> dict[str, Any] | None:
        """Extract table information from a SQLAlchemy model class."""
        try:
            table = model_class.__table__
            columns = []

            for col in table.columns:
                col_info = {
                    "name": col.name,
                    "type": col.type.__class__.__name__,
                    "primary_key": col.primary_key,
                    "nullable": col.nullable,
                    "default": None,
                }

                # Try to extract default value
                if col.default is not None:
                    if hasattr(col.default, "arg"):
                        col_info["default"] = col.default.arg
                    else:
                        col_info["default"] = str(col.default)

                columns.append(col_info)

            return {
                "table_name": table.name,
                "model_name": model_class.__name__,
                "columns": columns,
                "docstring": model_class.__doc__ or "",
            }
        except Exception as e:
            print(f"Warning: Failed to extract info from {model_class}: {e}")
            return None
