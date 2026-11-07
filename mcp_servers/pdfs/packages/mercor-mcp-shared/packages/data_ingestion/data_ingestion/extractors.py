"""Data extractor implementations.

This module provides concrete implementations of the DataExtractor interface
for common data formats like XML, JSON, and CSV.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, BinaryIO

from lxml import etree

from .exceptions import ConfigurationError, ExtractionError, ValidationError
from .interfaces import DataExtractor
from .utils import convert_type


@dataclass
class ExtractionResult:
    """Result of extracting a single record.

    Contains either a successfully extracted record or an error that occurred.
    This allows the extractor to continue processing even when individual records fail.

    Attributes:
        record: Extracted record data (None if extraction failed)
        error: Exception that occurred (None if extraction succeeded)
    """

    record: dict[str, Any] | None = None
    error: Exception | None = None

    @property
    def is_success(self) -> bool:
        """Check if extraction was successful (has record and no error)."""
        return self.error is None and self.record is not None

    @property
    def is_error(self) -> bool:
        """Check if extraction failed."""
        return self.error is not None


class XMLExtractor(DataExtractor):
    """XML data extractor with streaming support.

    Parses XML documents using streaming (SAX-style) approach to maintain
    constant memory usage. Field extraction is configuration-driven using
    structured field definitions with validation.

    Args:
        record_tags: List of XML tag names that represent records
        fields: Dictionary mapping field names to their extraction config
        namespaces: Optional XML namespace mappings

    Field Config Structure:
        Each field must have:
        - xpath: XPath expression to extract value
        - type: Data type (string, integer, float, boolean, list, date)
        - required: Whether field must be present (default: False)

    Example:
        >>> extractor = XMLExtractor(
        ...     record_tags=['patent-application'],
        ...     fields={
        ...         'app_number': {
        ...             'xpath': '//doc-number',
        ...             'type': 'string',
        ...             'required': True
        ...         },
        ...         'title': {
        ...             'xpath': '//invention-title',
        ...             'type': 'string',
        ...             'required': False
        ...         }
        ...     }
        ... )
        >>> for record in extractor.extract(xml_bytes):
        ...     print(record)
    """

    VALID_TYPES = {"string", "integer", "decimal", "date", "array"}

    def __init__(
        self,
        record_tags: list[str],
        fields: dict[str, dict[str, Any]],
        namespaces: dict[str, str] | None = None,
    ):
        """Initialize XMLExtractor.

        Args:
            record_tags: List of XML tag names that represent records
            fields: Dictionary mapping field names to extraction configuration
            namespaces: Optional XML namespace mappings

        Raises:
            ConfigurationError: If configuration is invalid
        """
        if not record_tags:
            raise ConfigurationError("record_tags cannot be empty")

        # Validate record_tags is a list, not a string
        # String would cause tuple() to split into characters
        if isinstance(record_tags, str):
            raise ConfigurationError(
                "record_tags must be a list, not a string. "
                f"Use record_tags: [{record_tags}] instead of record_tags: {record_tags}"
            )

        if not isinstance(record_tags, list):
            raise ConfigurationError("record_tags must be a list")

        if not fields:
            raise ConfigurationError("fields cannot be empty")

        # Validate fields is a dict, not a list or other type
        if not isinstance(fields, dict):
            raise ConfigurationError(f"fields must be a dict, got {type(fields).__name__}")

        self.record_tags = record_tags
        self.fields = fields
        self.namespaces = namespaces or {}

        # Validate field configurations
        self._validate_fields()

    def _validate_fields(self) -> None:
        """Validate all field configurations.

        Raises:
            ConfigurationError: If any field configuration is invalid
        """
        for field_name, config in self.fields.items():
            # Validate config is not None
            if config is None:
                raise ConfigurationError(f"Field '{field_name}' config cannot be None")

            # Validate required keys
            if "xpath" not in config:
                raise ConfigurationError(f"Field '{field_name}' missing required 'xpath' key")

            if "type" not in config:
                raise ConfigurationError(f"Field '{field_name}' missing required 'type' key")

            # Validate type
            if config["type"] not in self.VALID_TYPES:
                raise ConfigurationError(
                    f"Field '{field_name}' has invalid type '{config['type']}'. "
                    f"Valid types: {', '.join(sorted(self.VALID_TYPES))}"
                )

            # Validate XPath expression
            xpath_expr = config["xpath"]
            try:
                etree.XPath(xpath_expr, namespaces=self.namespaces)
            except etree.XPathSyntaxError as e:
                raise ConfigurationError(
                    f"Invalid XPath for field '{field_name}': {xpath_expr} - {e}"
                ) from e

            # Validate required (optional, defaults to False)
            if "required" in config and not isinstance(config["required"], bool):
                raise ConfigurationError(
                    f"Field '{field_name}' 'required' must be boolean, "
                    f"got {type(config['required'])}"
                )

            # Validate array_item_fields for array type
            if config["type"] == "array":
                if "array_item_fields" not in config:
                    raise ConfigurationError(
                        f"Field '{field_name}' with type 'array' missing required "
                        "'array_item_fields' key"
                    )
                # Validate each sub-field
                for sub_field_name, sub_config in config["array_item_fields"].items():
                    if "xpath" not in sub_config:
                        raise ConfigurationError(
                            f"Array item field '{field_name}.{sub_field_name}' "
                            "missing required 'xpath' key"
                        )
                    # Validate sub-field XPath
                    try:
                        etree.XPath(sub_config["xpath"], namespaces=self.namespaces)
                    except etree.XPathSyntaxError as e:
                        raise ConfigurationError(
                            f"Invalid XPath for array item field "
                            f"'{field_name}.{sub_field_name}': {sub_config['xpath']} - {e}"
                        ) from e

    def extract(self, file_handle: BinaryIO) -> Iterator[ExtractionResult]:
        """Extract records from XML file handle.

        Uses streaming XML parsing (iterparse) to process large documents
        with constant memory usage. Only keeps current record in memory.

        Args:
            file_handle: File handle (BinaryIO) to read XML data from

        Yields:
            ExtractionResult objects containing either:
            - Successful record (result.record contains data, result.error is None)
            - Failed record (result.record is None, result.error contains exception)

        Raises:
            ExtractionError: Only for file-level errors (invalid XML syntax, file not readable)
        """
        try:
            # Create streaming parser - lxml iterparse accepts file handles directly
            # This enables true streaming with minimal memory usage
            context = etree.iterparse(
                file_handle,
                events=("end",),
                tag=tuple(self.record_tags),
                recover=False,
            )

            # Process each record element
            for event, element in context:
                try:
                    record = self._extract_record(element)
                    # Success - yield record
                    yield ExtractionResult(record=record)
                except (ValidationError, ExtractionError) as e:
                    # Record-level error - yield error and continue processing
                    yield ExtractionResult(error=e)
                except Exception as e:
                    # Unexpected error - convert to ExtractionError and yield
                    extraction_error = ExtractionError(
                        f"Failed to extract record from <{element.tag}>: {e}"
                    )
                    yield ExtractionResult(error=extraction_error)
                finally:
                    # Always clear element to free memory (important for streaming)
                    element.clear()
                    while element.getprevious() is not None:
                        parent = element.getparent()
                        if parent is not None:
                            del parent[0]

        except etree.XMLSyntaxError as e:
            # Handle empty files gracefully - "no element found" just means no records
            if "no element found" in str(e).lower():
                return  # Empty file, no records to extract
            # Other XML syntax errors are fatal
            raise ExtractionError(f"Invalid XML syntax: {e}") from e
        except Exception as e:
            # Catch any other unexpected file-level errors
            if isinstance(e, ExtractionError):
                raise
            raise ExtractionError(f"XML extraction failed: {e}") from e

    def _extract_record(self, element: etree._Element) -> dict[str, Any]:
        """Extract fields from a record element using structured field configs.

        Args:
            element: XML element representing a single record

        Returns:
            Dictionary with extracted and type-converted field values

        Raises:
            ValidationError: If required field is missing
            ExtractionError: If XPath evaluation fails
        """
        record = {}

        for field_name, config in self.fields.items():
            xpath_expr = config["xpath"]
            field_type = config["type"]
            is_required = config.get("required", False)

            try:
                # Execute XPath expression
                result = element.xpath(xpath_expr, namespaces=self.namespaces)

                # Handle empty results
                if isinstance(result, list) and len(result) == 0:
                    if is_required:
                        raise ValidationError(f"Required field '{field_name}' not found in record")
                    # For arrays, set empty list; for other types, skip field entirely
                    # This allows factory defaults (data.get(key, default)) to work correctly
                    if field_type == "array":
                        record[field_name] = []
                    # Don't set key for None values - cleaner and enables factory defaults
                    continue

                # Handle array type with nested fields
                if field_type == "array":
                    array_items = []
                    array_item_fields = config["array_item_fields"]

                    # Ensure result is a list
                    elements = result if isinstance(result, list) else [result]

                    for item_element in elements:
                        item_dict = {}
                        for sub_field_name, sub_config in array_item_fields.items():
                            sub_xpath = sub_config["xpath"]
                            sub_result = item_element.xpath(sub_xpath, namespaces=self.namespaces)

                            if isinstance(sub_result, list) and len(sub_result) > 0:
                                item_dict[sub_field_name] = self._normalize_value(sub_result[0])
                            elif not isinstance(sub_result, list):
                                item_dict[sub_field_name] = self._normalize_value(sub_result)
                            else:
                                item_dict[sub_field_name] = None

                        # Skip array items where all fields are None
                        if any(v is not None for v in item_dict.values()):
                            array_items.append(item_dict)

                    record[field_name] = array_items
                    continue

                # Extract and normalize value for non-array types
                if isinstance(result, list):
                    if len(result) == 1:
                        raw_value = self._normalize_value(result[0])
                    else:
                        # Multiple values - return as list (shouldn't happen for non-array types)
                        raw_value = [self._normalize_value(v) for v in result]
                else:
                    raw_value = self._normalize_value(result)

                # Handle lists for non-array types (e.g., XPath union operators)
                # Take first element if list - this is XML-specific behavior
                if isinstance(raw_value, list):
                    if len(raw_value) == 0:
                        raw_value = None
                    else:
                        raw_value = raw_value[0]

                # Type conversion
                if raw_value is None:
                    if is_required:
                        raise ValidationError(f"Required field '{field_name}' has null value")
                    # Don't set key for None values - allows factory defaults to work
                    continue
                else:
                    record[field_name] = convert_type(raw_value, field_type, field_name)

            except ValidationError:
                raise
            except Exception as e:
                raise ExtractionError(
                    f"XPath evaluation failed for field '{field_name}': {xpath_expr} - {e}"
                ) from e

        return record

    def _normalize_value(self, value: Any) -> Any:
        """Normalize XPath result values.

        Converts lxml-specific types to standard Python types.

        Args:
            value: Raw value from XPath evaluation

        Returns:
            Normalized Python value
        """
        # Handle lxml element
        if isinstance(value, etree._Element):
            text = etree.tostring(value, encoding="unicode", method="text").strip()
            return text if text else None  # Return None for empty elements

        # Handle strings (most common case)
        if isinstance(value, str):
            stripped = value.strip()
            return stripped if stripped else None  # Return None for whitespace-only strings

        # Handle other types (bool, numbers, etc.)
        return value

    def supports_streaming(self) -> bool:
        """Check if extractor supports streaming.

        Returns:
            True (XMLExtractor uses streaming parser)
        """
        return True
