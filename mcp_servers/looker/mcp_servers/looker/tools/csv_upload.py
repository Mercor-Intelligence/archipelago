"""CSV upload tool for runtime data import.

This tool allows users to upload CSV files at runtime, which are then:
1. Saved to the state location (/.apps_data/looker/)
2. Converted to LookML view/model files
3. Loaded into DuckDB for querying

After upload, the data is immediately queryable via list_lookml_models,
get_explore, and run_query_inline.
"""

import base64
import csv
import io
import re
import sys
from pathlib import Path

# Add server to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp_schema import GeminiBaseModel as BaseModel
from pydantic import Field

# Regex for valid CSV filenames: alphanumeric, underscore, hyphen only
_VALID_FILENAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


def _sanitize_filename(filename: str) -> tuple[str, str | None]:
    """Sanitize and validate a CSV filename.

    Args:
        filename: Raw filename from user input

    Returns:
        Tuple of (sanitized_filename, error_message).
        If error_message is not None, the filename is invalid.
    """
    # Remove any directory components (prevent path traversal)
    basename = Path(filename).name

    # Remove .csv extension for validation (case-insensitive), will add back later
    # Handle both .csv and .CSV (common on Windows)
    if basename.lower().endswith(".csv"):
        name_without_ext = basename[:-4]  # Remove last 4 chars (.csv)
    else:
        name_without_ext = basename

    # Check for empty name
    if not name_without_ext:
        return "", "Filename cannot be empty"

    # Validate characters (only alphanumeric, underscore, hyphen)
    if not _VALID_FILENAME_PATTERN.match(name_without_ext):
        return "", (
            f"Invalid filename '{name_without_ext}'. "
            "Only letters, numbers, underscores, and hyphens are allowed."
        )

    # Return sanitized filename with .csv extension
    return f"{name_without_ext}.csv", None


class UploadCsvRequest(BaseModel):
    """Request to upload a CSV file and make it queryable."""

    filename: str = Field(
        ...,
        description=(
            "Name for the CSV file. The filename (without .csv extension) becomes the "
            "view/explore name. RESTRICTIONS: Only letters, numbers, underscores, and hyphens "
            "allowed (no spaces or special characters). The .csv extension is optional and "
            "will be added automatically."
        ),
        examples=["sales.csv", "customer_orders.csv", "q4-2024-revenue.csv"],
    )
    content_base64: str | None = Field(
        None,
        description=(
            "Base64-encoded CSV content. REQUIREMENTS: CSV must have a header row (column "
            "names) and at least one data row. Column names become field names in the "
            "generated explore. Either content_base64 or file_path must be provided, not both. "
            "To encode: base64.b64encode(csv_string.encode('utf-8')).decode('utf-8')"
        ),
        examples=["bmFtZSxhbW91bnQKSm9obiwxMDAKSmFuZSwyMDA="],  # "name,amount\nJohn,100\nJane,200"
    )
    file_path: str | None = Field(
        None,
        description="Path to an existing CSV file on the server filesystem "
        "(must be within STATE_LOCATION). Use this instead of content_base64 "
        "for large files to avoid base64 encoding overhead.",
    )


class UploadCsvResponse(BaseModel):
    """Response from CSV upload operation."""

    success: bool = Field(..., description="Whether the upload was successful")
    message: str = Field(..., description="Human-readable status message")
    model_name: str | None = Field(
        None, description="Name of the LookML model containing this data"
    )
    view_name: str | None = Field(
        None, description="Name of the view/explore created for this data"
    )
    row_count: int | None = Field(None, description="Number of data rows in the uploaded CSV")
    fields: list[str] | None = Field(None, description="List of field names detected in the CSV")


async def upload_csv(request: UploadCsvRequest) -> UploadCsvResponse:
    """Upload a CSV file and load into DuckDB for immediate querying."""
    from data_layer import add_single_view, get_user_csv_dir

    # Validate that either content_base64 or file_path is provided
    if not request.content_base64 and not request.file_path:
        return UploadCsvResponse(
            success=False,
            message="Either content_base64 or file_path must be provided",
        )

    # Sanitize and validate filename (prevents path traversal attacks)
    safe_filename, error = _sanitize_filename(request.filename)
    if error:
        return UploadCsvResponse(
            success=False,
            message=error,
        )

    # Get storage location (STATE_LOCATION in prod, temp dir in dev)
    state_location = get_user_csv_dir()

    if request.file_path:
        # File path mode: validate and use existing file
        source_path = Path(request.file_path)

        # Security check: ensure file is within state_location
        try:
            source_path.resolve().relative_to(state_location.resolve())
        except ValueError:
            return UploadCsvResponse(
                success=False,
                message="Invalid file_path: must be within STATE_LOCATION",
            )

        if not source_path.exists():
            return UploadCsvResponse(
                success=False,
                message=f"File not found: {request.file_path}",
            )

        # Read content for validation and field extraction
        try:
            content = source_path.read_text()
        except Exception as e:
            return UploadCsvResponse(
                success=False,
                message=f"Failed to read file: {e}",
            )

        csv_path = source_path
    else:
        # Base64 content mode: decode and save
        try:
            content = base64.b64decode(request.content_base64).decode("utf-8")
        except Exception as e:
            return UploadCsvResponse(
                success=False,
                message=f"Invalid base64 encoding: {e}",
            )

        # Validate CSV has content BEFORE writing to disk (avoids orphaned files)
        lines = content.strip().split("\n")
        if len(lines) < 2:
            return UploadCsvResponse(
                success=False,
                message="CSV must have at least a header row and one data row",
            )

        csv_path = state_location / safe_filename

        # Final safety check: ensure resolved path is within state_location
        try:
            csv_path.resolve().relative_to(state_location.resolve())
        except ValueError:
            return UploadCsvResponse(
                success=False,
                message="Invalid filename: path traversal detected",
            )

        try:
            csv_path.write_text(content)
        except Exception as e:
            return UploadCsvResponse(
                success=False,
                message=f"Failed to save CSV file: {e}",
            )

    # Validate CSV has content (for file_path mode)
    if request.file_path:
        lines = content.strip().split("\n")
        if len(lines) < 2:
            return UploadCsvResponse(
                success=False,
                message="CSV must have at least a header row and one data row",
            )

    # Extract header fields using proper CSV parsing (handles quoted fields with commas)
    header_line = lines[0]
    reader = csv.reader(io.StringIO(header_line))
    fields = next(reader)

    # Count data rows (excluding header)
    row_count = len(lines) - 1

    # Add single view incrementally (O(1) - doesn't rebuild everything)
    # Use csv_path.stem for view_name to ensure consistency with table name and field prefixes
    view_name = csv_path.stem
    add_single_view(view_name, csv_path)

    return UploadCsvResponse(
        success=True,
        message=f"Successfully uploaded {csv_path.name} with {row_count} rows. "
        f"Data is now queryable via model 'user_data', explore '{view_name}'.",
        model_name="user_data",
        view_name=view_name,
        row_count=row_count,
        fields=fields,
    )
