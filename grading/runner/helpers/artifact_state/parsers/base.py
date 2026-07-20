from abc import ABC, abstractmethod
from typing import Any, Literal

from pydantic import BaseModel, Field

# @apg_parser_type:start
ParserType = Literal["sexpr", "sql_inserts", "csv", "json"]
# @apg_parser_type:end


# @apg_table_mapping:start
class TableMapping(BaseModel):
    """Maps parsed data to an ArtifactState table.

    Each parser type uses different fields:
    - sexpr: node_type, extract_attrs, extract_properties
    - sql_inserts: source_table
    - csv: file_pattern, type_inference
    - json: json_path
    """

    table: str = Field(description="Output table name in ArtifactState")

    # sexpr parser fields
    node_type: str | None = Field(
        default=None,
        description="S-expression node type to extract (e.g., 'symbol', 'label')",
    )
    extract_attrs: list[str] = Field(
        default_factory=list,
        description="Node attributes to extract (e.g., ['lib_id', 'uuid'])",
    )
    extract_properties: list[str] = Field(
        default_factory=list,
        description="Property values to extract (e.g., ['Reference', 'Value'])",
    )

    # sql_inserts parser fields
    source_table: str | None = Field(
        default=None,
        description="SQL table name to extract INSERT rows from",
    )

    # csv parser fields
    file_pattern: str | None = Field(
        default=None,
        description="Filename pattern to match within glob results",
    )
    type_inference: bool = True  # Set False to keep all CSV values as strings

    # json parser fields
    json_path: str | None = Field(
        default=None,
        description="JSONPath expression to extract rows (e.g., '$.data.orders[*]')",
    )


# @apg_table_mapping:end


class BaseParser(ABC):
    """Abstract base class for artifact parsers.

    Each parser type (sexpr, sql_inserts, csv, json) implements this interface
    to provide config-driven artifact parsing without domain-specific code.
    """

    @abstractmethod
    def parse(self, content: str) -> Any:
        """Parse raw file content into an intermediate representation.

        Args:
            content: Raw text content of the artifact file.

        Returns:
            Parser-specific intermediate representation (e.g., parsed S-expr tree,
            list of SQL INSERT statements, CSV rows, JSON object).
        """

    @abstractmethod
    def apply_mapping(self, data: Any, mapping: TableMapping) -> list[dict[str, Any]]:
        """Extract rows from parsed data using the provided mapping config.

        Args:
            data: Intermediate representation from parse().
            mapping: Configuration specifying how to extract table rows.

        Returns:
            List of row dictionaries to add to the ArtifactState table.
        """
