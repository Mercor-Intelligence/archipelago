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
            tables = self._extract_models_from_ast(tree)
            if tables:
                return tables
            # AST returned empty - file may use imports/re-exports, try dynamic import
        except Exception as e:
            print(f"Warning: AST parsing failed, trying dynamic import: {e}")

        # Fallback to dynamic import (handles re-exported models)
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
                                    # Only use Mapped[...] type as fallback when mapped_column()
                                    # doesn't specify an explicit type (defaults to "String")
                                    if col_info["type"] == "String":
                                        mapped_type = self._extract_mapped_type(item.annotation)
                                        if mapped_type:
                                            col_info["type"] = mapped_type
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
        """Parse Column() or mapped_column() definition from AST."""
        col_type = None
        primary_key = False
        nullable = True
        default_value = None
        foreign_key_table: str | None = None

        # Non-type constraints that should not be treated as column types
        non_type_constraints = (
            "ForeignKey",
            "CheckConstraint",
            "Sequence",
            "Identity",
            "Computed",
            "Index",
            "UniqueConstraint",
            "PrimaryKeyConstraint",
        )

        # First positional argument is usually the type (for Column() syntax)
        # Only set type once - don't let later args overwrite it
        for arg in call_node.args:
            if isinstance(arg, ast.Name):
                # Simple type reference like Integer, String
                if col_type is None:
                    col_type = arg.id
            elif isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name):
                func_name = arg.func.id
                if func_name == "ForeignKey" and foreign_key_table is None:
                    # Extract referenced table from ForeignKey("table.column")
                    # Only use first FK if multiple exist (consistent with dynamic path)
                    foreign_key_table = self._extract_foreign_key_table(arg)
                elif func_name not in non_type_constraints and col_type is None:
                    # Type constructor like String(50), Numeric(10, 2)
                    col_type = func_name

        # Default to String if no type was detected
        if col_type is None:
            col_type = "String"

        # Check keyword arguments
        for keyword in call_node.keywords:
            if keyword.arg == "primary_key" and isinstance(keyword.value, ast.Constant):
                primary_key = keyword.value.value
            elif keyword.arg == "nullable" and isinstance(keyword.value, ast.Constant):
                nullable = keyword.value.value
            elif keyword.arg == "default" and isinstance(keyword.value, ast.Constant):
                default_value = keyword.value.value

        result = {
            "name": col_name,
            "type": col_type,
            "primary_key": primary_key,
            "nullable": nullable,
            "default": default_value,
        }

        # Add foreign key table if found
        if foreign_key_table:
            result["foreign_key_table"] = foreign_key_table

        return result

    def _extract_mapped_type(self, annotation: ast.expr | None) -> str | None:
        """
        Extract the Python type from a Mapped[...] annotation.

        Handles patterns like:
        - Mapped[int] -> "Integer"
        - Mapped[str] -> "String"
        - Mapped[str | None] -> "String"
        - Mapped[int | None] -> "Integer"
        """
        if annotation is None:
            return None

        # Map Python types to SQLAlchemy types
        type_mapping = {
            "int": "Integer",
            "str": "String",
            "float": "Float",
            "bool": "Boolean",
            "datetime": "DateTime",
            "date": "Date",
            "bytes": "LargeBinary",
        }

        # Handle Mapped[...] subscript
        if isinstance(annotation, ast.Subscript):
            if isinstance(annotation.value, ast.Name) and annotation.value.id == "Mapped":
                inner = annotation.slice
                return self._get_python_type_name(inner, type_mapping)

        return None

    def _get_python_type_name(self, node: ast.expr, type_mapping: dict) -> str | None:
        """Extract the primary type name from a type annotation node."""
        # Simple type: int, str, etc.
        if isinstance(node, ast.Name):
            return type_mapping.get(node.id, node.id)

        # Union type: int | None or str | None
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            # Get the non-None type from the union
            left_type = self._get_python_type_name(node.left, type_mapping)
            right_type = self._get_python_type_name(node.right, type_mapping)
            # Return the non-None type
            if left_type and left_type != "None":
                return left_type
            if right_type and right_type != "None":
                return right_type

        # Subscript type like Optional[int] or list[str]
        if isinstance(node, ast.Subscript):
            if isinstance(node.value, ast.Name):
                if node.value.id == "Optional":
                    return self._get_python_type_name(node.slice, type_mapping)
                # For other generics like list[str], return the outer type
                return node.value.id

        # Constant None
        if isinstance(node, ast.Constant) and node.value is None:
            return "None"

        return None

    def _extract_foreign_key_table(self, fk_call: ast.Call) -> str | None:
        """
        Extract the referenced table name from a ForeignKey() call.

        Parses patterns like:
        - ForeignKey("users.id") -> "users"
        - ForeignKey("job_requisitions.job_req_id") -> "job_requisitions"
        - ForeignKey("myschema.users.id") -> "users" (schema-qualified)
        """
        if not fk_call.args:
            return None

        first_arg = fk_call.args[0]
        if not isinstance(first_arg, ast.Constant) or not isinstance(first_arg.value, str):
            return None

        fk_ref = first_arg.value  # e.g., "users.id" or "schema.users.id"
        if "." in fk_ref:
            parts = fk_ref.split(".")
            # Table name is second-to-last (before column name)
            # "users.id" -> ["users", "id"] -> parts[-2] = "users"
            # "schema.users.id" -> ["schema", "users", "id"] -> parts[-2] = "users"
            return parts[-2]

        return None

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
            for name, obj in module.__dict__.items():
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

                # Extract foreign key table from SQLAlchemy column metadata
                if col.foreign_keys:
                    for fk in col.foreign_keys:
                        # fk.target_fullname is like "users.id" or "schema.users.id"
                        # Can be None for unresolved FKs
                        target = fk.target_fullname
                        if target and "." in target:
                            parts = target.split(".")
                            # Table name is second-to-last (before column name)
                            table_name = parts[-2]
                            if table_name:  # Guard against malformed refs like ".column"
                                col_info["foreign_key_table"] = table_name
                        break  # Only use first FK

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
