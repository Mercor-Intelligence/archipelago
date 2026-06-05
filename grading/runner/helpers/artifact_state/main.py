import fnmatch
import io
import zipfile
from typing import Any

from runner.helpers.artifact_state.models import ArtifactState
from runner.helpers.artifact_state.parsers import (
    BaseParser,
    CSVParser,
    JSONParser,
    SExprParser,
    SQLInsertParser,
)
from runner.helpers.artifact_state.parsers.base import ParserType, TableMapping
from runner.helpers.trace_utils import matches_glob
from runner.models import AgentTrajectoryOutput

PARSER_REGISTRY: dict[ParserType, type[BaseParser]] = {
    "sexpr": SExprParser,
    "sql_inserts": SQLInsertParser,
    "csv": CSVParser,
    "json": JSONParser,
}


async def artifact_state_helper(
    initial_snapshot_bytes: io.BytesIO,
    final_snapshot_bytes: io.BytesIO,
    trajectory: AgentTrajectoryOutput,
    *,
    parser_config: dict[str, Any] | None = None,
) -> ArtifactState:
    """Extract structured artifact state using config-driven parsing.

    This helper reads artifact files from the final snapshot, parses them
    using the specified parser type, and maps the results to tables based
    on the provided table mappings.

    Args:
        initial_snapshot_bytes: Initial snapshot zip (unused but required
                                 by helper interface for consistency).
        final_snapshot_bytes: Final snapshot zip containing artifact files.
        trajectory: Agent trajectory output (required by helper interface).
        parser_config: Configuration for parsing, containing:
                       - parser: Parser type ("sexpr", "sql_inserts", "csv", "json")
                       - file_glob: Glob pattern to find artifact files
                       - table_mappings: List of TableMapping dicts

    Returns:
        ArtifactState containing extracted tables and metadata.
    """
    # If no parser_config provided, return empty state
    # This allows the helper to be called with the standard signature
    if parser_config is None:
        return ArtifactState(
            parse_errors=["No parser_config provided to artifact_state_helper"],
        )

    parser_type: ParserType = parser_config["parser"]
    file_glob: str = parser_config["file_glob"]
    table_mappings_raw: list[dict[str, Any]] = parser_config.get("table_mappings", [])

    # Convert raw mappings to TableMapping
    table_mappings = [TableMapping(**m) for m in table_mappings_raw]

    # Initialize parser
    parser_class = PARSER_REGISTRY.get(parser_type)
    if not parser_class:
        return ArtifactState(
            parse_errors=[f"Unknown parser type: {parser_type}"],
        )

    parser = parser_class()

    # Extract matching files from snapshot
    tables: dict[str, list[dict[str, Any]]] = {}
    source_files: list[str] = []
    parse_errors: list[str] = []

    final_snapshot_bytes.seek(0)
    try:
        with zipfile.ZipFile(final_snapshot_bytes, "r") as zf:
            for name in zf.namelist():
                info = zf.getinfo(name)
                if info.is_dir():
                    continue

                # Check if file matches glob pattern
                if not matches_glob(name, file_glob):
                    continue

                source_files.append(name)

                # Extract and parse file content
                try:
                    content = zf.read(name).decode("utf-8", errors="replace")
                    parsed_data = parser.parse(content)

                    # Apply each table mapping
                    for mapping in table_mappings:
                        # For CSV parser, check file_pattern if specified
                        if parser_type == "csv" and mapping.file_pattern:
                            if not fnmatch.fnmatch(name, mapping.file_pattern):
                                continue

                        rows = parser.apply_mapping(parsed_data, mapping)
                        if mapping.table not in tables:
                            tables[mapping.table] = []
                        tables[mapping.table].extend(rows)

                except Exception as e:
                    parse_errors.append(f"Error parsing {name}: {e!s}")

    except Exception as e:
        parse_errors.append(f"Error reading snapshot: {e!s}")

    return ArtifactState(
        tables=tables,
        source_files=source_files,
        parse_errors=parse_errors,
    )
