import json
from typing import Any

from jsonpath_ng import parse as jsonpath_parse

from .base import BaseParser, TableMapping


class JSONParser(BaseParser):
    """Parser for JSON files with JSONPath extraction.

    Uses JSONPath expressions to extract rows from JSON structures.
    For example, '$.data.orders[*]' extracts all items from data.orders array.
    """

    def parse(self, content: str) -> Any:
        """Parse JSON content.

        Args:
            content: Raw JSON text.

        Returns:
            Parsed JSON structure (dict, list, etc.).
        """
        return json.loads(content)

    def apply_mapping(self, data: Any, mapping: TableMapping) -> list[dict[str, Any]]:
        """Extract rows from JSON using JSONPath expression.

        Args:
            data: Parsed JSON data.
            mapping: Must include json_path specifying what to extract.

        Returns:
            List of dictionaries extracted from the JSON.
        """
        if not mapping.json_path:
            return []

        jsonpath_expr = jsonpath_parse(mapping.json_path)
        matches = jsonpath_expr.find(data)

        rows: list[dict[str, Any]] = []
        for match in matches:
            value = match.value
            if isinstance(value, dict):
                rows.append(value)
            elif isinstance(value, list):
                # If the match is a list, expand it
                for item in value:
                    if isinstance(item, dict):
                        rows.append(item)
                    else:
                        rows.append({"value": item})
            else:
                # Scalar value - wrap in dict
                rows.append({"value": value})

        return rows
