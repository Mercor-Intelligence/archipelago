"""Shared CSV import/validation REST endpoints and Pydantic models.

Provides reusable REST endpoints for CSV file upload, validation, and import.
Each MCP server keeps a thin ui_csv_endpoints.py wrapper that passes its
SQLAlchemy Base to the shared register_csv_endpoints() function.

Usage in a server's ui_csv_endpoints.py:

    from db.models import Base
    from mcp_scripts.csv_endpoints import register_csv_endpoints

    def register_endpoints(app, module_path, engine=None):
        register_csv_endpoints(app, Base, engine)
"""

from __future__ import annotations

import base64
import csv
import io
import shutil
import tempfile
import zipfile
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import text

from mcp_scripts.import_csv import (
    FileValidationResult as CoreFileValidationResult,
)
from mcp_scripts.import_csv import (
    ImportResult as CoreImportResult,
)
from mcp_scripts.import_csv import (
    SchemaIntrospector,
    SchemaResponse,
    import_csvs,
    is_valid_csv_file,
    to_schema_response,
    validate_csvs,
)
from mcp_scripts.import_csv import (
    ValidationError as CoreValidationError,
)
from mcp_scripts.import_csv import (
    ValidationResult as CoreValidationResult,
)

# =============================================================================
# Pydantic Request/Response Models
# =============================================================================


class ValidationErrorResponse(BaseModel):
    """A single validation error."""

    file: str = Field(..., description="CSV filename where the error occurred")
    error_type: str = Field(
        ...,
        description="Error category: READ_ERROR, NO_HEADERS, NO_TABLE_MATCH, "
        "MISSING_REQUIRED, NULL_VALUE, TYPE_ERROR, FK_VIOLATION, DUPLICATE_TABLE",
    )
    message: str = Field(..., description="Human-readable error description")
    row: int | None = Field(None, description="Row number (starting from 2, after header)")
    column: str | None = Field(None, description="Column name where the error occurred")


class FileValidationResultResponse(BaseModel):
    """Validation result for a single CSV file."""

    file_name: str = Field(..., description="CSV filename")
    table_name: str | None = Field(None, description="Matched database table name")
    success: bool = Field(..., description="Whether validation passed for this file")
    row_count: int = Field(0, description="Number of data rows in the file")
    errors: list[ValidationErrorResponse] = Field(
        default_factory=list, description="List of validation errors for this file"
    )
    sample_rows: list[dict[str, str]] = Field(
        default_factory=list, description="First 3 rows of data for preview"
    )


class ValidationResponse(BaseModel):
    """Complete validation response for a ZIP file."""

    success: bool = Field(..., description="Whether all files passed validation")
    files_total: int = Field(..., description="Total number of CSV files processed")
    files_valid: int = Field(..., description="Number of files that passed validation")
    files_invalid: int = Field(..., description="Number of files with validation errors")
    total_errors: int = Field(..., description="Total error count across all files")
    fk_violations: int = Field(..., description="Number of foreign key violations")
    files: list[FileValidationResultResponse] = Field(
        default_factory=list, description="Per-file validation results"
    )
    fk_errors: list[ValidationErrorResponse] = Field(
        default_factory=list, description="Cross-file foreign key violations"
    )


class FileImportResultResponse(BaseModel):
    """Import result for a single CSV file."""

    file_name: str = Field(..., description="CSV filename")
    table_name: str = Field(..., description="Database table imported into")
    rows_imported: int = Field(..., description="Number of rows imported")


class ImportResponse(BaseModel):
    """Complete import response for a ZIP file."""

    success: bool = Field(..., description="Whether import completed successfully")
    files_imported: int = Field(..., description="Number of files imported")
    total_rows: int = Field(..., description="Total rows imported across all files")
    files: list[FileImportResultResponse] = Field(
        default_factory=list, description="Per-file import results"
    )
    tables_cleared: list[str] | None = Field(
        default=None, description="Tables that had existing data cleared before import"
    )
    needs_confirmation: bool = Field(
        default=False, description="True if existing data found and confirmation needed"
    )
    existing_data_tables: list[str] | None = Field(
        default=None, description="Tables with existing data (when needs_confirmation=True)"
    )
    message: str = Field(..., description="Summary message")


class ValidateRequest(BaseModel):
    """Request model for CSV validation."""

    file_content: str = Field(..., description="Base64-encoded ZIP file content")
    filename: str | None = Field(None, description="Original filename (optional)")


class ImportRequest(BaseModel):
    """Request model for CSV import."""

    file_content: str = Field(..., description="Base64-encoded ZIP file content")
    filename: str | None = Field(None, description="Original filename (optional)")
    confirm_clear: bool = Field(
        default=False, description="Confirm clearing existing data if tables already have data"
    )


# =============================================================================
# Core-to-API Converters
# =============================================================================


def convert_error(err: CoreValidationError) -> ValidationErrorResponse:
    """Convert core ValidationError to API response model."""
    return ValidationErrorResponse(
        file=err.file,
        error_type=err.error_type,
        message=err.message,
        row=err.row,
        column=err.column,
    )


def _read_sample_rows(csv_path: Path, max_rows: int = 3) -> list[dict[str, str]]:
    """Read the first N rows from a CSV file for preview."""
    sample_rows = []
    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader):
                if i >= max_rows:
                    break
                # Convert all values to strings and handle None
                sample_rows.append({k: (v or "") for k, v in row.items()})
    except Exception:
        # If we can't read the file, just return empty list
        pass
    return sample_rows


def convert_file_result(result: CoreFileValidationResult) -> FileValidationResultResponse:
    """Convert core FileValidationResult to API response model."""
    return FileValidationResultResponse(
        file_name=result.csv_path.name,
        table_name=result.table_name,
        success=result.success,
        row_count=result.row_count,
        errors=[convert_error(e) for e in result.errors],
        sample_rows=_read_sample_rows(result.csv_path),
    )


def to_validation_response(result: CoreValidationResult) -> ValidationResponse:
    """Convert core ValidationResult to REST API response."""
    # Group FK errors by file to count files with any issues
    fk_errors_by_file: dict[str, list] = {}
    for e in result.fk_errors:
        fk_errors_by_file.setdefault(e.file, []).append(e)

    # Count files as invalid if they have file errors OR FK errors
    valid_count = sum(
        1 for f in result.files if f.success and f.csv_path.name not in fk_errors_by_file
    )
    invalid_count = len(result.files) - valid_count

    return ValidationResponse(
        success=result.success,
        files_total=len(result.files),
        files_valid=valid_count,
        files_invalid=invalid_count,
        total_errors=result.total_errors,
        fk_violations=len(result.fk_errors),
        files=[convert_file_result(f) for f in result.files],
        fk_errors=[convert_error(e) for e in result.fk_errors],
    )


def to_import_response(result: CoreImportResult) -> ImportResponse:
    """Convert core ImportResult to REST API response."""
    # Build success message
    if result.needs_confirmation:
        tables_count = len(result.existing_data_tables or [])
        plural = "s" if tables_count > 1 else ""
        message = (
            f"Warning: Found existing data in {tables_count} table{plural}. "
            "Click 'Confirm' to clear existing data and import."
        )
    elif result.error_message:
        message = result.error_message
    else:
        message = (
            f"Successfully imported {len(result.files)} files "
            f"with {result.total_rows_imported} total rows"
        )
        # Add note about cleared tables if any
        if result.tables_cleared:
            cleared_count = len(result.tables_cleared)
            plural = "s" if cleared_count > 1 else ""
            message += f" (cleared existing data from {cleared_count} table{plural})"

    return ImportResponse(
        success=result.success,
        files_imported=len(result.files),
        total_rows=result.total_rows_imported,
        files=[
            FileImportResultResponse(
                file_name=f.file_name,
                table_name=f.table_name,
                rows_imported=f.rows_imported,
            )
            for f in result.files
        ],
        tables_cleared=result.tables_cleared,
        needs_confirmation=result.needs_confirmation,
        existing_data_tables=result.existing_data_tables,
        message=message,
    )


# =============================================================================
# ZIP Extraction Helper
# =============================================================================


def _extract_zip_to_temp(base64_content: str, prefix: str = "csv_") -> str:
    """Decode base64 ZIP and extract CSV files to temp directory.

    Uses is_valid_csv_file from mcp_scripts.import_csv for consistent
    case-insensitive CSV detection across the codebase.

    Args:
        base64_content: Base64-encoded ZIP file content
        prefix: Prefix for the temp directory name

    Returns:
        Path to temporary directory containing extracted CSV files

    Raises:
        HTTPException: If base64 decoding fails, ZIP is invalid, or no CSV files found
    """
    try:
        file_data = base64.b64decode(base64_content)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid base64 encoding: {e}")

    try:
        zip_buffer = io.BytesIO(file_data)
        with zipfile.ZipFile(zip_buffer, "r") as zf:
            csv_files = [f for f in zf.namelist() if is_valid_csv_file(f)]
            if not csv_files:
                raise HTTPException(status_code=400, detail="ZIP file contains no CSV files")
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Invalid ZIP file format")

    temp_dir = tempfile.mkdtemp(prefix=prefix)
    try:
        zip_buffer.seek(0)
        with zipfile.ZipFile(zip_buffer, "r") as zf:
            for csv_name in csv_files:
                csv_content = zf.read(csv_name)
                csv_path = Path(temp_dir) / Path(csv_name).name
                csv_path.write_bytes(csv_content)
    except Exception as e:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"Failed to extract CSV files: {e}")

    return temp_dir


# =============================================================================
# Endpoint Registration
# =============================================================================


def register_csv_endpoints(app: FastAPI, base: type, engine=None) -> None:
    """Register CSV import/validation REST endpoints on a FastAPI app.

    Args:
        app: FastAPI application instance
        base: SQLAlchemy declarative base with registered models
        engine: SQLAlchemy async engine for database imports
    """
    if engine is None:
        raise ValueError("engine parameter is required for CSV import endpoints")

    @app.get("/schema", response_model=SchemaResponse)
    async def schema_endpoint() -> SchemaResponse:
        """Get the database schema for frontend consumption.

        Returns full schema information including tables, columns, types,
        FK relationships, required columns, and topological import order.
        """
        return to_schema_response(base)

    @app.post("/validate", response_model=ValidationResponse)
    async def validate_endpoint(request: ValidateRequest) -> ValidationResponse:
        """Validate CSV files in a ZIP archive.

        Uses the same validation code as mcp_scripts/import_csv.py.
        """
        temp_dir = _extract_zip_to_temp(request.file_content)

        try:
            result = validate_csvs(Path(temp_dir), base)
            return to_validation_response(result)
        except Exception as e:
            logger.error(f"Validation failed: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Validation failed: {e}")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    @app.post("/import-validated", response_model=ImportResponse)
    async def import_endpoint(request: ImportRequest) -> ImportResponse:
        """Validate and import CSV files from a ZIP archive.

        Uses the same validation and import code as mcp_scripts/import_csv.py.
        If existing data is found, returns needs_confirmation=True.
        Re-submit with confirm_clear=True to proceed.
        """
        temp_dir = _extract_zip_to_temp(request.file_content)

        try:
            result = await import_csvs(
                Path(temp_dir), engine, base, confirm_clear=request.confirm_clear
            )

            # Return confirmation response without error (let UI handle it)
            if result.needs_confirmation:
                return to_import_response(result)

            if not result.success:
                error_msg = (
                    result.error_message or f"{result.validation.total_errors} validation error(s)"
                )
                raise HTTPException(status_code=400, detail=error_msg)

            return to_import_response(result)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Import failed: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Import failed: {e}")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    @app.get("/export-snapshot")
    async def export_snapshot_endpoint() -> StreamingResponse:
        """Export all database tables as CSV files in a ZIP archive.

        Each table is exported as a separate CSV file named <table_name>.csv.
        """
        from sqlalchemy.ext.asyncio import AsyncSession

        introspector = SchemaIntrospector(base)

        zip_buffer = io.BytesIO()
        try:
            async with AsyncSession(engine) as session:
                with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                    for table_name in introspector.tables:
                        result = await session.execute(
                            text(f"SELECT * FROM {table_name}")  # noqa: S608
                        )
                        col_names = list(result.keys())
                        rows = result.fetchall()

                        csv_buffer = io.StringIO()
                        writer = csv.writer(csv_buffer)
                        writer.writerow(col_names)
                        for row in rows:
                            writer.writerow(["" if val is None else val for val in row])

                        zf.writestr(f"{table_name}.csv", csv_buffer.getvalue())

                        logger.debug(f"Exported {len(rows)} rows from {table_name}")
        except Exception as e:
            logger.error(f"Export snapshot failed: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Export snapshot failed: {e}")

        zip_buffer.seek(0)
        return StreamingResponse(
            zip_buffer,
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="snapshot.zip"'},
        )
