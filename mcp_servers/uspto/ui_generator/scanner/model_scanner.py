"""
Model Scanner - Extract Pydantic models from models.py files for documentation.
"""

import ast
import importlib.util
import inspect
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel


class ModelInfo:
    """Information about a Pydantic model."""

    def __init__(
        self,
        name: str,
        docstring: str | None,
        fields: dict[str, Any],
        bases: list[str],
        is_enum: bool = False,
    ):
        self.name = name
        self.docstring = docstring
        self.fields = fields
        self.bases = bases
        self.is_enum = is_enum


class ModelScanner:
    """Scan models.py files to extract Pydantic model definitions."""

    def scan_models_file(self, filepath: str | Path) -> list[ModelInfo]:
        """
        Scan a models.py file and extract all Pydantic model definitions.

        Args:
            filepath: Path to models.py file

        Returns:
            List of ModelInfo objects
        """
        filepath = Path(filepath)

        if not filepath.exists():
            raise FileNotFoundError(f"Models file not found: {filepath}")

        # Try dynamic import first (more accurate)
        try:
            models = self._scan_dynamic(filepath)
            if models:
                return models
        except Exception as e:
            print(f"Warning: Dynamic import failed, falling back to AST: {e}")

        # Fall back to AST parsing
        return self._scan_ast(filepath)

    def _scan_dynamic(self, filepath: Path) -> list[ModelInfo]:
        """Dynamically import and scan the models file."""
        spec = importlib.util.spec_from_file_location("temp_models", filepath)
        if not spec or not spec.loader:
            return []

        module = importlib.util.module_from_spec(spec)
        sys.modules["temp_models"] = module

        try:
            spec.loader.exec_module(module)
            models = []

            for name, obj in inspect.getmembers(module):
                # Skip private/imported items
                if name.startswith("_"):
                    continue

                # Check if it's a Pydantic model
                if inspect.isclass(obj) and issubclass(obj, BaseModel) and obj is not BaseModel:
                    model_info = self._extract_model_info(obj)
                    if model_info:
                        models.append(model_info)
                # Check if it's an Enum
                elif inspect.isclass(obj) and hasattr(obj, "__bases__"):
                    import enum

                    if any(
                        issubclass(base, enum.Enum)
                        for base in obj.__bases__
                        if inspect.isclass(base)
                    ):
                        model_info = self._extract_enum_info(obj)
                        if model_info:
                            models.append(model_info)

            return models
        finally:
            # Clean up
            if "temp_models" in sys.modules:
                del sys.modules["temp_models"]

    def _extract_model_info(self, model_class: type[BaseModel]) -> ModelInfo | None:
        """Extract information from a Pydantic model class."""
        try:
            # Get model fields
            fields = {}
            if hasattr(model_class, "model_fields"):
                for field_name, field_info in model_class.model_fields.items():
                    fields[field_name] = {
                        "type": self._format_type(field_info.annotation),
                        "required": field_info.is_required(),
                        "description": field_info.description or "",
                        "default": self._get_default_value(field_info)
                        if not field_info.is_required()
                        else None,
                    }

            # Get base classes
            bases = [base.__name__ for base in model_class.__bases__ if base is not BaseModel]

            return ModelInfo(
                name=model_class.__name__,
                docstring=inspect.getdoc(model_class),
                fields=fields,
                bases=bases,
                is_enum=False,
            )
        except Exception as e:
            print(f"Warning: Failed to extract info for {model_class.__name__}: {e}")
            return None

    def _extract_enum_info(self, enum_class) -> ModelInfo | None:
        """Extract information from an Enum class."""
        try:
            fields = {}
            for member in enum_class:
                value = member.value
                fields[member.name] = {
                    "type": "str" if isinstance(value, str) else type(value).__name__,
                    "required": True,
                    "description": f'Value: "{value}"'
                    if isinstance(value, str)
                    else f"Value: {value}",
                    "default": None,
                }

            return ModelInfo(
                name=enum_class.__name__,
                docstring=inspect.getdoc(enum_class),
                fields=fields,
                bases=[base.__name__ for base in enum_class.__bases__],
                is_enum=True,
            )
        except Exception as e:
            print(f"Warning: Failed to extract enum info for {enum_class.__name__}: {e}")
            return None

    def _scan_ast(self, filepath: Path) -> list[ModelInfo]:
        """Scan using AST parsing (fallback method)."""
        with open(filepath) as f:
            source = f.read()

        tree = ast.parse(source)
        models = []

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                # Check if it inherits from BaseModel or Enum
                is_pydantic = any(self._is_basemodel_base(base) for base in node.bases)
                is_enum = any(self._is_enum_base(base) for base in node.bases)

                if is_pydantic or is_enum:
                    model_info = self._extract_from_ast(node, is_enum)
                    if model_info:
                        models.append(model_info)

        return models

    def _extract_from_ast(self, node: ast.ClassDef, is_enum: bool) -> ModelInfo | None:
        """Extract model info from AST ClassDef node."""
        fields = {}

        # Get docstring
        docstring = ast.get_docstring(node)

        # Extract fields
        for item in node.body:
            # Handle annotated assignments (Pydantic models): field_name: type = value
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                field_name = item.target.id
                field_type = self._ast_type_to_string(item.annotation)

                # Try to extract Field() description
                description = ""
                default = None
                required = True

                if item.value:
                    if isinstance(item.value, ast.Call):
                        # Check if it's Field()
                        if isinstance(item.value.func, ast.Name) and item.value.func.id == "Field":
                            for keyword in item.value.keywords:
                                if keyword.arg == "description" and isinstance(
                                    keyword.value, ast.Constant
                                ):
                                    description = keyword.value.value
                                elif keyword.arg == "default":
                                    default = self._ast_value_to_string(keyword.value)
                                    required = False
                                elif keyword.arg == "default_factory":
                                    # Handle default_factory (e.g., default_factory=list)
                                    factory_name = self._ast_value_to_string(keyword.value)
                                    default = f"{factory_name}()"
                                    required = False
                        else:
                            default = self._ast_value_to_string(item.value)
                            required = False
                    else:
                        default = self._ast_value_to_string(item.value)
                        required = False

                fields[field_name] = {
                    "type": field_type,
                    "required": required,
                    "description": description,
                    "default": default,
                }

            # Handle simple assignments: MEMBER_NAME = "value" or MEMBER_NAME = auto()
            elif is_enum and isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name):
                        field_name = target.id
                        # Skip dunder attributes
                        if field_name.startswith("_"):
                            continue

                        # Determine the value and type
                        value_str = self._ast_value_to_string(item.value)
                        if isinstance(item.value, ast.Constant):
                            value = item.value.value
                            value_type = "str" if isinstance(value, str) else type(value).__name__
                            description = (
                                f'Value: "{value}"' if isinstance(value, str) else f"Value: {value}"
                            )
                        elif isinstance(item.value, ast.Call):
                            # Handle auto() or other function calls
                            value_type = "auto"
                            description = f"Value: {value_str}"
                        else:
                            value_type = "Any"
                            description = f"Value: {value_str}"

                        fields[field_name] = {
                            "type": value_type,
                            "required": True,
                            "description": description,
                            "default": None,
                        }

        # Get base classes
        bases = [self._ast_type_to_string(base) for base in node.bases]

        return ModelInfo(
            name=node.name,
            docstring=docstring,
            fields=fields,
            bases=bases,
            is_enum=is_enum,
        )

    def _is_basemodel_base(self, base: ast.expr) -> bool:
        """Check if base is BaseModel."""
        if isinstance(base, ast.Name):
            return base.id == "BaseModel"
        return False

    def _is_enum_base(self, base: ast.expr) -> bool:
        """Check if base is an Enum."""
        if isinstance(base, ast.Name):
            return "Enum" in base.id
        if isinstance(base, ast.Attribute):
            return "Enum" in base.attr
        return False

    def _ast_type_to_string(self, annotation: ast.expr) -> str:
        """Convert AST type annotation to string."""
        if isinstance(annotation, ast.Name):
            return annotation.id
        elif isinstance(annotation, ast.Constant):
            return repr(annotation.value)
        elif isinstance(annotation, ast.Subscript):
            value = self._ast_type_to_string(annotation.value)
            slice_val = self._ast_type_to_string(annotation.slice)
            return f"{value}[{slice_val}]"
        elif isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
            left = self._ast_type_to_string(annotation.left)
            right = self._ast_type_to_string(annotation.right)
            return f"{left} | {right}"
        elif isinstance(annotation, ast.Attribute):
            return f"{self._ast_type_to_string(annotation.value)}.{annotation.attr}"
        elif isinstance(annotation, ast.Tuple):
            elements = [self._ast_type_to_string(el) for el in annotation.elts]
            return f"({', '.join(elements)})"
        return "Any"

    def _ast_value_to_string(self, value: ast.expr) -> str:
        """Convert AST value to string representation."""
        if isinstance(value, ast.Constant):
            return repr(value.value)
        elif isinstance(value, ast.Name):
            return value.id
        elif isinstance(value, ast.List):
            return "[]"
        elif isinstance(value, ast.Dict):
            return "{}"
        return "..."

    def _format_type(self, annotation) -> str:
        """Format type annotation for display."""
        if annotation is None:
            return "Any"

        # Handle string annotations
        if isinstance(annotation, str):
            return annotation

        # Get the string representation
        type_str = str(annotation)

        # Clean up common patterns
        type_str = type_str.replace("typing.", "")
        type_str = type_str.replace("<class '", "").replace("'>", "")

        # Normalize Union[X, None] or X | None to Optional[X]
        # This ensures consistent representation regardless of how the type was written
        import re

        # Match pattern: SomeType | None or Union[SomeType, None]
        union_none_pattern = r"^(.+?)\s*\|\s*None$"
        match = re.match(union_none_pattern, type_str)
        if match:
            inner_type = match.group(1).strip()
            type_str = f"Optional[{inner_type}]"

        # Normalize ForwardRef by removing is_class parameter
        # ForwardRef('X', is_class=True) -> ForwardRef('X')
        forwardref_pattern = r"ForwardRef\(([^,]+),\s*is_class=\w+\)"
        type_str = re.sub(forwardref_pattern, r"ForwardRef(\1)", type_str)

        return type_str

    def _get_default_value(self, field_info) -> str | None:
        """
        Extract default value from field_info, handling default_factory and Pydantic sentinels.

        Args:
            field_info: Pydantic FieldInfo object

        Returns:
            String representation of default value, or None if no default
        """
        try:
            # Check for default_factory first (e.g., Field(default_factory=list))
            if hasattr(field_info, "default_factory") and field_info.default_factory is not None:
                factory = field_info.default_factory
                # Try to get a meaningful name for the factory
                if hasattr(factory, "__name__"):
                    factory_name = factory.__name__
                    # Common factories like list, dict, set
                    if factory_name in ("list", "dict", "set"):
                        return f"{factory_name}()"
                    return f"{factory_name}()"
                return "factory()"

            # Check if default exists and is not a Pydantic sentinel (e.g., PydanticUndefined)
            if hasattr(field_info, "default"):
                default = field_info.default
                # Skip Pydantic internal types (sentinels like PydanticUndefined)
                if str(type(default).__name__).startswith("Pydantic"):
                    return None
                # Handle explicit None defaults and other values
                return self._format_default(default)
        except Exception:
            # Skip defaults that cause issues
            pass

        return None

    def _format_default(self, default) -> str:
        """Format default value for display."""
        if default is None:
            return "None"
        # Check for Pydantic internal types (safety check)
        if str(type(default).__name__).startswith("Pydantic"):
            return None
        if isinstance(default, str):
            return f'"{default}"'
        if isinstance(default, list | dict | set):
            return repr(default)
        return str(default)

    def export_to_dict(self, models: list[ModelInfo]) -> list[dict[str, Any]]:
        """Export models to dictionary format for templates."""
        return [
            {
                "name": model.name,
                "docstring": model.docstring or "",
                "fields": model.fields,
                "bases": model.bases,
                "is_enum": model.is_enum,
            }
            for model in models
        ]
