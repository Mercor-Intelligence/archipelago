"""
Pydantic Parser - Extract schema information from Pydantic models.
"""

import inspect
from typing import Any, get_args, get_origin

from pydantic import BaseModel
from pydantic.fields import FieldInfo


class PydanticParser:
    """Parse Pydantic models into structured schema dictionaries."""

    def parse_model(self, model_class: type[BaseModel]) -> dict[str, Any]:
        """
        Parse a Pydantic model into structured schema.

        Args:
            model_class: Pydantic BaseModel class to parse

        Returns:
            Dictionary containing model metadata and field information
        """
        if not issubclass(model_class, BaseModel):
            raise ValueError(f"{model_class} is not a Pydantic BaseModel")

        fields = []
        for field_name, field_info in model_class.model_fields.items():
            field_schema = self._parse_field(field_name, field_info)
            fields.append(field_schema)

        return {
            "model_name": model_class.__name__,
            "description": self._get_docstring(model_class),
            "fields": fields,
        }

    def _parse_field(self, field_name: str, field_info: FieldInfo) -> dict[str, Any]:
        """Parse a single Pydantic field."""
        annotation = field_info.annotation

        field_schema = {
            "name": field_name,
            "type": self._get_field_type_string(annotation),
            "python_type": annotation,
            "required": field_info.is_required(),
            "description": field_info.description or "",
            "validators": self._extract_validators(field_info),
        }

        # Extract examples
        if hasattr(field_info, "examples") and field_info.examples:
            field_schema["examples"] = field_info.examples

        # Extract default (avoid Pydantic internal types)
        try:
            if (
                hasattr(field_info, "default")
                and field_info.default is not None
                and not str(type(field_info.default).__name__).startswith("Pydantic")
            ):
                # Check if it's not the default factory
                has_default_factory = hasattr(field_info, "default_factory")
                is_not_factory = (
                    not has_default_factory or field_info.default != field_info.default_factory
                )
                if is_not_factory:
                    field_schema["default"] = field_info.default
            elif hasattr(field_info, "default_factory") and field_info.default_factory is not None:
                field_schema["default_factory"] = str(field_info.default_factory)
        except Exception:
            # Skip defaults that cause issues
            pass

        # Handle nested models
        origin = get_origin(annotation)
        if origin is None and inspect.isclass(annotation) and issubclass(annotation, BaseModel):
            field_schema["nested_model"] = annotation.__name__
            field_schema["nested_schema"] = self.parse_model(annotation)
        elif origin in (list, tuple):
            args = get_args(annotation)
            if args and inspect.isclass(args[0]) and issubclass(args[0], BaseModel):
                field_schema["nested_model"] = args[0].__name__
                field_schema["nested_schema"] = self.parse_model(args[0])

        return field_schema

    def _get_field_type_string(self, annotation: Any) -> str:
        """Convert Python type annotation to string representation."""
        origin = get_origin(annotation)

        # Handle None type (Optional)
        if annotation is type(None):
            return "None"

        # Handle basic types
        if annotation in (str, int, float, bool):
            return annotation.__name__

        # Handle typing constructs
        if origin is not None:
            args = get_args(annotation)

            # Optional[X] -> Union[X, None]
            is_union = origin is type(None) or (
                hasattr(origin, "__name__") and origin.__name__ == "UnionType"
            )
            if is_union:
                # This is Optional or Union
                type_args = [
                    self._get_field_type_string(arg) for arg in args if arg is not type(None)
                ]
                if len(type_args) == 1:
                    return f"Optional[{type_args[0]}]"
                return f"Union[{', '.join(type_args)}]"

            # List[X]
            if origin is list:
                if args:
                    return f"List[{self._get_field_type_string(args[0])}]"
                return "List"

            # Dict[K, V]
            if origin is dict:
                if args and len(args) == 2:
                    key_type = self._get_field_type_string(args[0])
                    val_type = self._get_field_type_string(args[1])
                    return f"Dict[{key_type}, {val_type}]"
                return "Dict"

            # Tuple
            if origin is tuple:
                if args:
                    arg_types = [self._get_field_type_string(arg) for arg in args]
                    return f"Tuple[{', '.join(arg_types)}]"
                return "Tuple"

        # Handle date/datetime
        if hasattr(annotation, "__name__"):
            if annotation.__name__ in ("date", "datetime", "time"):
                return annotation.__name__

        # Handle Literal
        is_literal = (
            hasattr(annotation, "__class__")
            and annotation.__class__.__name__ == "_LiteralGenericAlias"
        )
        if is_literal:
            args = get_args(annotation)
            return f"Literal{args}"

        # Handle Enum
        if inspect.isclass(annotation) and issubclass(annotation, BaseModel):
            return annotation.__name__

        # Fallback
        return str(annotation)

    def _extract_validators(self, field_info: FieldInfo) -> dict[str, Any]:
        """Extract Pydantic validators (min, max, regex, etc.)."""
        validators = {}

        # Access constraints from field metadata
        if hasattr(field_info, "metadata"):
            for constraint in field_info.metadata:
                if hasattr(constraint, "min_length"):
                    validators["min_length"] = constraint.min_length
                if hasattr(constraint, "max_length"):
                    validators["max_length"] = constraint.max_length
                if hasattr(constraint, "gt"):
                    validators["gt"] = constraint.gt
                if hasattr(constraint, "ge"):
                    validators["ge"] = constraint.ge
                if hasattr(constraint, "lt"):
                    validators["lt"] = constraint.lt
                if hasattr(constraint, "le"):
                    validators["le"] = constraint.le
                if hasattr(constraint, "pattern"):
                    validators["pattern"] = constraint.pattern

        return validators

    def _get_docstring(self, obj: Any) -> str:
        """Extract docstring from an object."""
        doc = inspect.getdoc(obj)
        return doc.strip() if doc else ""
