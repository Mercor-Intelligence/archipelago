import re
from typing import Any

from .base import BaseParser, TableMapping


class SExprParser(BaseParser):
    """Parser for S-expression files (e.g., KiCad schematics).

    S-expressions are LISP-like nested list structures:
        (symbol (lib_id "Device:R") (property "Reference" "R1"))

    This parser extracts nodes by type and extracts attributes and properties
    based on the mapping configuration.
    """

    def parse(self, content: str) -> list[Any]:
        """Parse S-expression content into a nested list structure.

        Args:
            content: Raw S-expression text.

        Returns:
            Nested list representation of the S-expression.
        """
        return self._parse_sexpr(content)

    def apply_mapping(self, data: Any, mapping: TableMapping) -> list[dict[str, Any]]:
        """Extract rows from S-expression data matching the node type.

        Args:
            data: Parsed S-expression (nested list).
            mapping: Must include node_type; optionally extract_attrs and
                     extract_properties.

        Returns:
            List of dictionaries with extracted attributes and properties.
        """
        if not mapping.node_type:
            return []

        nodes = self._find_nodes_by_type(data, mapping.node_type)
        rows: list[dict[str, Any]] = []

        for node in nodes:
            row: dict[str, Any] = {}

            # Extract attributes (direct children that are strings/values)
            for attr in mapping.extract_attrs:
                value = self._get_attribute(node, attr)
                if value is not None:
                    row[attr] = value

            # Extract properties (children like (property "Name" "Value"))
            for prop_name in mapping.extract_properties:
                value = self._get_property(node, prop_name)
                if value is not None:
                    row[prop_name] = value

            if row:
                rows.append(row)

        return rows

    def _parse_sexpr(self, text: str) -> list[Any]:
        """Parse S-expression text into nested Python lists."""
        tokens = self._tokenize(text)
        result, _ = self._parse_tokens(tokens, 0)
        return result

    def _tokenize(self, text: str) -> list[str]:
        """Tokenize S-expression text into individual tokens."""
        # Match: quoted strings, parentheses, or non-whitespace sequences
        pattern = r'"(?:[^"\\]|\\.)*"|[()]|[^\s()"]+'
        return re.findall(pattern, text)

    def _parse_tokens(self, tokens: list[str], index: int) -> tuple[list[Any], int]:
        """Recursively parse tokens into nested list structure."""
        result: list[Any] = []
        i = index

        while i < len(tokens):
            token = tokens[i]

            if token == "(":
                # Start of a new list
                sublist, i = self._parse_tokens(tokens, i + 1)
                result.append(sublist)
            elif token == ")":
                # End of current list
                return result, i + 1
            else:
                # Atom: strip quotes if present
                if token.startswith('"') and token.endswith('"'):
                    token = token[1:-1]
                result.append(token)
                i += 1

        return result, i

    def _find_nodes_by_type(self, data: Any, node_type: str) -> list[list[Any]]:
        """Find all nodes (lists) where the first element matches node_type."""
        results: list[list[Any]] = []

        if isinstance(data, list):
            if len(data) > 0 and data[0] == node_type:
                results.append(data)
            for item in data:
                results.extend(self._find_nodes_by_type(item, node_type))

        return results

    def _get_attribute(self, node: list[Any], attr_name: str) -> Any | None:
        """Get an attribute value from a node.

        Attributes can be:
        - Direct child: (symbol "value") -> attr_name="symbol" returns None
        - Sub-node with value: (symbol (lib_id "Device:R")) -> attr_name="lib_id"
        """
        for item in node:
            if isinstance(item, list) and len(item) >= 2 and item[0] == attr_name:
                return item[1]
        return None

    def _get_property(self, node: list[Any], prop_name: str) -> Any | None:
        """Get a property value from a node.

        Properties have the form: (property "PropertyName" "PropertyValue" ...)
        """
        for item in node:
            if (
                isinstance(item, list)
                and len(item) >= 3
                and item[0] == "property"
                and item[1] == prop_name
            ):
                return item[2]
        return None
