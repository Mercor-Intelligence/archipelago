"""JSON response parser using JSONPath expressions.

This module provides config-driven parsing of JSON API responses,
similar to how XMLExtractor uses XPath for XML documents.
"""

from dataclasses import dataclass, field
from typing import Any

from jsonpath_ng import parse as jsonpath_parse
from jsonpath_ng.exceptions import JsonPathParserError

from ..exceptions import ConfigurationError, ExtractionError


@dataclass
class FieldConfig:
    """Configuration for a single field extraction.

    Attributes:
        path: JSONPath expression to extract value (e.g., "@.name", "@.folder")
        field_type: Data type for conversion (string, boolean, integer)
    """

    path: str
    field_type: str = "string"

    def __post_init__(self):
        if self.field_type not in ("string", "boolean", "integer"):
            raise ConfigurationError(
                f"Invalid field type '{self.field_type}'. Valid types: string, boolean, integer"
            )


@dataclass
class ParserConfig:
    """Configuration for JSON response parsing.

    Attributes:
        items_path: JSONPath to the array of items (e.g., "$.files")
        item_fields: Dictionary mapping field names to their extraction config
    """

    items_path: str
    item_fields: dict[str, FieldConfig] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, config: dict) -> "ParserConfig":
        """Create ParserConfig from dictionary (parsed YAML).

        Args:
            config: Dictionary with parser configuration

        Returns:
            ParserConfig instance

        Raises:
            ConfigurationError: If configuration is invalid
        """
        if not config:
            raise ConfigurationError("Parser config cannot be empty")

        if "items_path" not in config:
            raise ConfigurationError("Parser config missing required 'items_path'")

        items_path = config["items_path"]
        item_fields = {}

        if "item_fields" in config:
            for name, field_config in config["item_fields"].items():
                if isinstance(field_config, dict):
                    item_fields[name] = FieldConfig(
                        path=field_config.get("path", f"@.{name}"),
                        field_type=field_config.get("type", "string"),
                    )
                else:
                    raise ConfigurationError(
                        f"Field '{name}' config must be a dict, got {type(field_config).__name__}"
                    )

        return cls(items_path=items_path, item_fields=item_fields)


@dataclass
class ParsedItem:
    """A single item parsed from the API response.

    Attributes:
        name: Item name/filename
        url: URL to the item (for download or further crawling)
        is_folder: Whether this item is a folder (needs further crawling)
        size: File size in bytes (optional)
        last_modified: Last modification timestamp (optional)
        raw_data: Original raw data from the response
    """

    name: str
    url: str
    is_folder: bool = False
    size: int | None = None
    last_modified: str | None = None
    raw_data: dict[str, Any] = field(default_factory=dict)


class ResponseParser:
    """Parse JSON API responses using configured JSONPath expressions.

    This parser extracts items from JSON responses based on configuration,
    similar to how XMLExtractor uses XPath for XML documents.

    Example:
        >>> config = ParserConfig.from_dict({
        ...     "items_path": "$.files",
        ...     "item_fields": {
        ...         "name": {"path": "@.name"},
        ...         "url": {"path": "@.link"},
        ...         "is_folder": {"path": "@.folder", "type": "boolean"}
        ...     }
        ... })
        >>> parser = ResponseParser(config)
        >>> items = parser.parse({"files": [{"name": "test.xml", "link": "http://..."}]})
    """

    def __init__(self, config: ParserConfig):
        """Initialize ResponseParser.

        Args:
            config: Parser configuration with JSONPath expressions

        Raises:
            ConfigurationError: If JSONPath expressions are invalid
        """
        self.config = config

        # Pre-compile JSONPath expressions for performance
        try:
            self._items_path = jsonpath_parse(config.items_path)
        except JsonPathParserError as e:
            raise ConfigurationError(
                f"Invalid JSONPath for items_path '{config.items_path}': {e}"
            ) from e

        self._field_paths: dict[str, Any] = {}
        for name, field_config in config.item_fields.items():
            try:
                # Handle @ prefix for current item context
                path = field_config.path
                if path.startswith("@."):
                    # Convert @.field to $.field for jsonpath-ng
                    path = "$" + path[1:]
                self._field_paths[name] = (jsonpath_parse(path), field_config.field_type)
            except JsonPathParserError as e:
                raise ConfigurationError(
                    f"Invalid JSONPath for field '{name}': {field_config.path} - {e}"
                ) from e

    def parse(self, response: dict) -> list[ParsedItem]:
        """Parse JSON response and extract items.

        Args:
            response: JSON response dictionary

        Returns:
            List of ParsedItem objects

        Raises:
            ExtractionError: If parsing fails
        """
        try:
            # Find all items using the items_path
            matches = self._items_path.find(response)

            if not matches:
                return []

            items = []
            for match in matches:
                item_data = match.value

                # Handle case where items_path matches an array
                if isinstance(item_data, list):
                    for single_item in item_data:
                        parsed = self._extract_item(single_item)
                        if parsed:
                            items.append(parsed)
                else:
                    parsed = self._extract_item(item_data)
                    if parsed:
                        items.append(parsed)

            return items

        except Exception as e:
            if isinstance(e, ConfigurationError | ExtractionError):
                raise
            raise ExtractionError(f"Failed to parse JSON response: {e}") from e

    def _extract_item(self, item_data: dict) -> ParsedItem | None:
        """Extract a single item from raw data.

        Args:
            item_data: Dictionary containing item data

        Returns:
            ParsedItem or None if extraction fails
        """
        if not isinstance(item_data, dict):
            return None

        extracted = {"raw_data": item_data}

        for field_name, (path_expr, field_type) in self._field_paths.items():
            matches = path_expr.find(item_data)
            if matches:
                raw_value = matches[0].value
                extracted[field_name] = self._convert_type(raw_value, field_type, field_name)
            else:
                extracted[field_name] = None

        # Build ParsedItem with required fields
        name = extracted.get("name") or ""
        url = extracted.get("url") or ""

        if not url:
            return None

        return ParsedItem(
            name=str(name),
            url=str(url),
            is_folder=bool(extracted.get("is_folder", False)),
            size=extracted.get("size"),
            last_modified=extracted.get("last_modified"),
            raw_data=item_data,
        )

    def _convert_type(self, value: Any, field_type: str, field_name: str) -> Any:
        """Convert value to specified type.

        Args:
            value: Raw value from JSON
            field_type: Target type (string, boolean, integer)
            field_name: Field name for error messages

        Returns:
            Converted value
        """
        if value is None:
            return None

        try:
            if field_type == "boolean":
                if isinstance(value, bool):
                    return value
                if isinstance(value, str):
                    return value.lower() in ("true", "1", "yes")
                return bool(value)

            elif field_type == "integer":
                if isinstance(value, int):
                    return value
                if isinstance(value, str):
                    # Handle empty strings
                    if not value.strip():
                        return None
                    return int(value)
                return int(value)

            else:  # string
                return str(value) if value is not None else None

        except (ValueError, TypeError) as e:
            raise ExtractionError(
                f"Failed to convert field '{field_name}' value '{value}' to {field_type}: {e}"
            ) from e
