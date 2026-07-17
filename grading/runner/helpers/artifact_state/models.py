from typing import Any

from pydantic import BaseModel, Field


class ArtifactState(BaseModel):
    """Unified state model for artifact verification.

    All artifact parsing produces this same structure, regardless of the
    underlying file format (KiCad, SQL dumps, CSV, JSON). This enables
    generic assertion evaluation against the extracted data.

    Attributes:
        tables: Mapping from table name to list of row dictionaries.
                Each row is a dict with field names as keys.
        source_files: List of file paths that were parsed to produce this state.
        parse_errors: List of non-fatal parsing errors encountered, if any.
    """

    tables: dict[str, list[dict[str, Any]]] = Field(
        default_factory=dict,
        description="Table name -> list of row dictionaries",
    )
    source_files: list[str] = Field(
        default_factory=list,
        description="Source file paths that were parsed",
    )
    parse_errors: list[str] = Field(
        default_factory=list,
        description="Non-fatal parsing errors encountered",
    )
