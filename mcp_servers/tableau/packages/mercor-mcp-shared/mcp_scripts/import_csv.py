#!/usr/bin/env python3
"""Schema-Aware CSV Import Script for Greenhouse

This script imports CSV files with full schema awareness:
- Introspects SQLAlchemy models to understand tables, columns, and FK relationships
- Maps CSV headers to tables (header-based, not filename-based)
- Topologically sorts tables by FK dependencies
- Validates data types and FK references before import
- Fails fast with detailed error reporting

Usage:
    python import_csv.py --dir /path/to/csvs [--validate-only] [--db /path/to/db]

Supported Table Categories:

Users:
    - users, user_emails, departments, offices, user_departments, user_offices

Jobs:
    - jobs, job_departments, job_offices, hiring_team, job_stages
    - interview_steps, interview_kit_questions, interview_step_default_interviewers
    - job_openings

Candidates:
    - candidates, candidate_phone_numbers, candidate_email_addresses
    - candidate_addresses, candidate_website_addresses, candidate_social_media_addresses
    - candidate_educations, candidate_employments, candidate_attachments
    - tags, candidate_tags

Applications:
    - applications, application_answers, rejection_reasons

Scorecards:
    - scorecards, scorecard_attributes, scorecard_questions

Activity:
    - notes, emails, activities

Job Board:
    - job_posts, job_post_questions, job_post_question_options
    - prospect_pools, prospect_pool_stages, degrees, disciplines, schools

Sources:
    - source_types, sources
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import DeclarativeBase


def is_valid_csv_file(path: Path | str) -> bool:
    """Check if a path is a valid CSV file (case-insensitive, excludes macOS metadata).

    This is the shared logic for determining which files should be processed as CSVs.
    Used by both the core import logic and the API layer's ZIP extraction.

    Args:
        path: File path to check

    Returns:
        True if the file has a .csv extension (case-insensitive) and is not macOS metadata
    """
    name = Path(path).name if isinstance(path, str) else path.name
    path_str = str(path)
    is_csv = name.lower().endswith(".csv")
    is_macos_metadata = "__MACOSX" in path_str or name.startswith("._")
    return is_csv and not is_macos_metadata


def _find_csv_files(directory: Path) -> list[Path]:
    """Find all valid CSV files in a directory (case-insensitive)."""
    all_files = list(directory.iterdir())
    return sorted(f for f in all_files if f.is_file() and is_valid_csv_file(f))


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class ColumnInfo:
    """Information about a table column."""

    name: str
    python_type: type
    nullable: bool
    is_primary_key: bool
    is_foreign_key: bool
    fk_target: str | None = None
    has_default: bool = False  # True if column has default or server_default
    default_value: Any = None  # Extracted Python default value (from ORM model)
    enum_values: list[str] | None = None  # Valid values from info dict
    is_unique: bool = False  # True if column has unique constraint
    date_after: str | None = None  # Date must be after this column


@dataclass
class TableInfo:
    """Information about a database table."""

    name: str
    columns: dict[str, ColumnInfo]
    primary_keys: list[str]
    foreign_keys: dict[str, str]
    required_columns: list[str]
    # List of column groups that must be unique together
    unique_constraints: list[list[str]] = field(default_factory=list)


@dataclass
class ValidationError:
    """A validation error."""

    file: str
    error_type: str
    message: str
    row: int | None = None
    column: str | None = None


@dataclass
class FileValidationResult:
    """Result of validating a single CSV file (internal)."""

    csv_path: Path
    table_name: str | None
    success: bool
    errors: list[ValidationError] = field(default_factory=list)
    row_count: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass
class ValidationResult:
    """Complete validation result returned by validate_csvs()."""

    success: bool
    files: list[FileValidationResult]
    fk_errors: list[ValidationError]
    total_errors: int
    schema: SchemaIntrospector


@dataclass
class FileImportResult:
    """Import result for a single CSV file."""

    file_name: str
    table_name: str
    rows_imported: int


@dataclass
class ImportResult:
    """Complete import result returned by import_csvs()."""

    success: bool
    validation: ValidationResult
    files: list[FileImportResult]
    total_rows_imported: int
    error_message: str | None = None
    tables_cleared: list[str] | None = None  # Tables that had existing data cleared
    needs_confirmation: bool = False  # True if existing data found and clear not confirmed
    existing_data_tables: list[str] | None = None  # Tables with existing data (for confirm)


# =============================================================================
# Schema Response Models (Pydantic)
# =============================================================================


class ColumnSchemaResponse(BaseModel):
    """Schema information for a single column."""

    name: str = Field(..., description="Column name")
    type: str = Field(..., description="Python type name (str, int, float, bool, datetime, dict)")
    nullable: bool = Field(..., description="Whether the column allows NULL values")
    is_primary_key: bool = Field(False, description="Whether this column is a primary key")
    is_foreign_key: bool = Field(False, description="Whether this column is a foreign key")
    fk_target: str | None = Field(None, description="Foreign key target (table.column) if FK")
    required: bool = Field(False, description="Value required (non-nullable, no default)")
    enum_values: list[str] | None = Field(None, description="Valid enum values from model info")
    is_unique: bool = Field(False, description="Whether this column has a unique constraint")
    date_after: str | None = Field(None, description="Date must be after the referenced column")


class TableSchemaResponse(BaseModel):
    """Schema information for a single table."""

    name: str = Field(..., description="Table name")
    columns: list[ColumnSchemaResponse] = Field(..., description="List of columns in the table")
    primary_keys: list[str] = Field(..., description="List of primary key column names")
    foreign_keys: dict[str, str] = Field(
        ..., description="Map of FK column names to their targets (table.column)"
    )
    required_columns: list[str] = Field(
        ..., description="Columns that require a value (non-nullable, no default)"
    )
    unique_constraints: list[list[str]] = Field(
        default_factory=list,
        description="List of unique constraints, each as a list of column names",
    )


class SchemaResponse(BaseModel):
    """Complete database schema response for frontend consumption."""

    tables: list[TableSchemaResponse] = Field(..., description="List of all tables in the schema")
    import_order: list[str] = Field(
        ..., description="Tables in topological order (parent tables first)"
    )
    table_count: int = Field(..., description="Total number of tables")


# =============================================================================
# Schema Introspector
# =============================================================================


class SchemaIntrospector:
    """Introspects SQLAlchemy models to extract schema information."""

    def __init__(self, base: type[DeclarativeBase]):
        self.base = base
        self.tables: dict[str, TableInfo] = {}
        self._introspect()

    def _introspect(self) -> None:
        """Introspect all registered models."""
        for mapper in self.base.registry.mappers:
            model = mapper.class_
            if hasattr(model, "__tablename__"):
                table_info = self._introspect_model(model)
                self.tables[table_info.name] = table_info

    def _introspect_model(self, model: Any) -> TableInfo:
        """Extract schema info from a single model."""
        from sqlalchemy import UniqueConstraint

        table = model.__table__
        columns: dict[str, ColumnInfo] = {}
        primary_keys: list[str] = []
        foreign_keys: dict[str, str] = {}
        required_columns: list[str] = []
        unique_constraints: list[list[str]] = []

        # Collect column-level unique constraints
        single_unique_cols: set[str] = set()

        for col in table.columns:
            python_type = self._sqlalchemy_type_to_python(col.type)

            fk_target = None
            is_fk = bool(col.foreign_keys)
            if is_fk:
                fk = list(col.foreign_keys)[0]
                fk_target = str(fk.target_fullname)
                foreign_keys[col.name] = fk_target

            has_default = col.default is not None or col.server_default is not None

            # Extract Python default value (scalar only)
            # Callable defaults (like utc_now) are handled by SQLAlchemy/database
            # via server_default, so we don't need to invoke them manually
            default_value = None
            if col.default is not None and col.default.is_scalar:
                default_value = col.default.arg

            # Get metadata from column info dict if present
            # Models define this as: mapped_column(..., info={"enum": [...], "date_after": "..."})
            enum_values = col.info.get("enum") if col.info else None
            date_after = col.info.get("date_after") if col.info else None

            # Check for column-level unique constraint
            is_unique = col.unique is True
            if is_unique:
                single_unique_cols.add(col.name)

            col_info = ColumnInfo(
                name=col.name,
                python_type=python_type,
                nullable=col.nullable if col.nullable is not None else True,
                is_primary_key=col.primary_key,
                is_foreign_key=is_fk,
                fk_target=fk_target,
                has_default=has_default,
                default_value=default_value,
                enum_values=enum_values,
                is_unique=is_unique,
                date_after=date_after,
            )
            columns[col.name] = col_info

            if col.primary_key:
                primary_keys.append(col.name)
            elif not col.nullable and not has_default:
                required_columns.append(col.name)

        # Add single-column unique constraints
        for col_name in single_unique_cols:
            unique_constraints.append([col_name])

        # Extract table-level UniqueConstraint from __table_args__
        for constraint in table.constraints:
            if isinstance(constraint, UniqueConstraint):
                constraint_cols = [col.name for col in constraint.columns]
                # Avoid duplicates from column-level unique=True
                if constraint_cols not in unique_constraints:
                    unique_constraints.append(constraint_cols)

        return TableInfo(
            name=table.name,
            columns=columns,
            primary_keys=primary_keys,
            foreign_keys=foreign_keys,
            required_columns=required_columns,
            unique_constraints=unique_constraints,
        )

    def _sqlalchemy_type_to_python(self, sa_type: Any) -> type:
        """Convert SQLAlchemy type to Python type."""
        type_name = type(sa_type).__name__.upper()

        if "INT" in type_name or "BIGINT" in type_name:
            return int
        elif "FLOAT" in type_name or "NUMERIC" in type_name or "DECIMAL" in type_name:
            return float
        elif "BOOL" in type_name:
            return bool
        elif "DATE" in type_name or "TIME" in type_name:
            return datetime
        elif "JSON" in type_name:
            return dict
        else:
            return str

    def get_topological_order(self) -> list[str]:
        """Get tables in topological order (dependencies first).

        Returns tables ordered so that parent tables (those referenced by FKs)
        come before child tables (those with FKs).

        Note: Self-referential FKs and circular dependencies are handled
        by excluding them from the dependency graph.
        """
        # Build adjacency list: parent -> children (tables that depend on parent)
        children: dict[str, set[str]] = {name: set() for name in self.tables}
        in_degree: dict[str, int] = {name: 0 for name in self.tables}

        for table_name, table_info in self.tables.items():
            # Collect unique parent tables (a table may have multiple FKs to same parent)
            parent_tables: set[str] = set()
            for fk_target in table_info.foreign_keys.values():
                parent_table = fk_target.split(".")[0]
                # Exclude self-referential FKs and unknown tables
                if parent_table in self.tables and parent_table != table_name:
                    parent_tables.add(parent_table)

            # Add edges and increment in_degree once per unique parent
            for parent_table in parent_tables:
                children[parent_table].add(table_name)
                in_degree[table_name] += 1

        # Kahn's algorithm: start with tables that have no dependencies
        queue = [name for name, degree in in_degree.items() if degree == 0]
        result: list[str] = []

        while queue:
            node = queue.pop(0)
            result.append(node)

            for child in children[node]:
                in_degree[child] -= 1
                if in_degree[child] == 0:
                    queue.append(child)

        # Handle circular dependencies: add remaining tables
        if len(result) != len(self.tables):
            remaining = set(self.tables.keys()) - set(result)
            logger.warning(f"Circular FK dependencies detected, adding remaining: {remaining}")
            result.extend(sorted(remaining))

        return result


# =============================================================================
# CSV Matcher
# =============================================================================


class CSVMatcher:
    """Matches CSV files to database tables based on headers."""

    def __init__(self, schema: SchemaIntrospector):
        self.schema = schema

    def normalize_header(self, header: str) -> str:
        """Normalize a header name for comparison."""
        normalized = header.lower().strip()
        normalized = re.sub(r"[\s\-\.]+", "_", normalized)
        normalized = re.sub(r"[^a-z0-9_]", "", normalized)
        return normalized

    def match_csv_to_table(
        self, headers: list[str], csv_filename: str | None = None
    ) -> tuple[str | None, float, list[str] | None]:
        """Find the best matching table for given CSV headers.

        Args:
            headers: List of CSV column headers
            csv_filename: Optional filename (without extension) to use as tiebreaker
                         when multiple tables have equal scores

        Returns:
            Tuple of (table_name, match_score, ambiguous_tables) where:
            - table_name: Matched table or None if no match/ambiguous
            - match_score: Confidence score (0.0-1.0)
            - ambiguous_tables: List of tied table names if ambiguous, None otherwise
        """
        if not headers:
            return None, 0.0, None

        normalized_headers = {self.normalize_header(h) for h in headers}

        # Collect all matches with scores
        matches: list[tuple[str, float]] = []

        for table_name, table_info in self.schema.tables.items():
            table_columns = set(table_info.columns.keys())

            matched = normalized_headers & table_columns
            if not matched:
                continue

            csv_coverage = len(matched) / len(normalized_headers)
            table_coverage = len(matched) / len(table_columns)

            auto_columns = {"id", "created_at", "updated_at"}
            required = set(table_info.required_columns) - auto_columns
            required_present = required & normalized_headers
            required_score = len(required_present) / len(required) if required else 1.0

            # Weight: 30% csv coverage, 30% table coverage, 40% required columns
            score = (csv_coverage * 0.3) + (table_coverage * 0.3) + (required_score * 0.4)

            if score > 0:
                matches.append((table_name, score))

        if not matches:
            return None, 0.0, None

        # Sort by score descending
        matches.sort(key=lambda x: x[1], reverse=True)

        best_score = matches[0][1]

        # Get all tables with the best score (potential ties)
        top_matches = [m for m in matches if m[1] == best_score]

        # Single match - no ambiguity
        if len(top_matches) == 1:
            return top_matches[0][0], top_matches[0][1], None

        # Multiple matches - try exact filename tiebreaker
        if csv_filename:
            normalized_filename = self.normalize_header(csv_filename)
            for table_name, score in top_matches:
                if table_name == normalized_filename:
                    logger.debug(f"Filename tiebreaker: '{csv_filename}' -> '{table_name}'")
                    return table_name, score, None

        # Ambiguous: multiple tables with same score, no exact filename match
        tied_tables = [t[0] for t in top_matches]
        logger.debug(f"Ambiguous match: {tied_tables}")
        return None, best_score, tied_tables


# =============================================================================
# CSV Validator
# =============================================================================


class CSVValidator:
    """Validates CSV files against the database schema."""

    def __init__(self, schema: SchemaIntrospector, matcher: CSVMatcher):
        self.schema = schema
        self.matcher = matcher
        self.id_registry: dict[str, set[str]] = defaultdict(set)

    def read_csv(self, csv_path: Path) -> tuple[list[str], list[dict[str, str]]]:
        """Read CSV file and return headers and rows."""
        with open(csv_path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            headers = list(reader.fieldnames or [])
            rows = list(reader)
        return headers, rows

    def validate_csv(self, csv_path: Path) -> FileValidationResult:
        """Validate a single CSV file."""
        errors: list[ValidationError] = []
        warnings: list[str] = []

        try:
            headers, rows = self.read_csv(csv_path)
        except Exception as e:
            return FileValidationResult(
                csv_path=csv_path,
                table_name=None,
                success=False,
                errors=[
                    ValidationError(
                        file=csv_path.name,
                        error_type="READ_ERROR",
                        message=str(e),
                    )
                ],
            )

        if not headers:
            return FileValidationResult(
                csv_path=csv_path,
                table_name=None,
                success=False,
                errors=[
                    ValidationError(
                        file=csv_path.name,
                        error_type="NO_HEADERS",
                        message="CSV file has no headers",
                    )
                ],
            )

        table_name, match_score, ambiguous_tables = self.matcher.match_csv_to_table(
            headers, csv_filename=csv_path.stem
        )

        # Handle ambiguous match - multiple tables with same score
        if ambiguous_tables:
            suggestions = ", ".join(f"{t}.csv" for t in sorted(ambiguous_tables))
            return FileValidationResult(
                csv_path=csv_path,
                table_name=None,
                success=False,
                errors=[
                    ValidationError(
                        file=csv_path.name,
                        error_type="AMBIGUOUS_MATCH",
                        message=(
                            f"Multiple tables match with same score ({match_score:.1%}). "
                            f"Rename file to one of: {suggestions}"
                        ),
                    )
                ],
            )

        if not table_name or match_score < 0.3:
            return FileValidationResult(
                csv_path=csv_path,
                table_name=None,
                success=False,
                errors=[
                    ValidationError(
                        file=csv_path.name,
                        error_type="NO_TABLE_MATCH",
                        message=(
                            f"Could not match CSV headers to any table "
                            f"(best score: {match_score:.1%}). "
                            f"Headers: {headers[:5]}{'...' if len(headers) > 5 else ''}"
                        ),
                    )
                ],
            )

        logger.info(f"  Matched {csv_path.name} → {table_name} (score: {match_score:.1%})")

        table_info = self.schema.tables[table_name]
        normalized_headers = {self.matcher.normalize_header(h): h for h in headers}

        auto_columns = {"id", "created_at", "updated_at"}
        required = set(table_info.required_columns) - auto_columns
        missing_required = required - set(normalized_headers.keys())

        if missing_required:
            errors.append(
                ValidationError(
                    file=csv_path.name,
                    error_type="MISSING_REQUIRED",
                    message=f"Missing required columns: {sorted(missing_required)}",
                )
            )

        # Track composite PK values to detect duplicates
        # For composite PKs like (candidate_id, tag_id), the COMBINATION must be unique
        seen_pk_tuples: set[tuple[str, ...]] = set()

        # Track unique constraint values to detect duplicates
        # Key: tuple of constraint column names, Value: set of seen value tuples
        seen_unique_values: dict[tuple[str, ...], set[tuple[str, ...]]] = {
            tuple(constraint): set() for constraint in table_info.unique_constraints
        }

        for row_num, row in enumerate(rows, start=2):
            row_errors = self._validate_row(
                csv_path.name, row_num, row, table_info, normalized_headers
            )
            errors.extend(row_errors)

            # Build the composite PK tuple for this row
            pk_values: list[str] = []
            pk_columns_present = True
            for pk in table_info.primary_keys:
                if pk in normalized_headers:
                    orig_header = normalized_headers[pk]
                    val = row.get(orig_header, "").strip()
                    if val:
                        pk_values.append(val)
                    else:
                        pk_columns_present = False
                        break
                else:
                    pk_columns_present = False
                    break

            # Only check for duplicates if all PK columns are present and non-empty
            if pk_columns_present and pk_values:
                pk_tuple = tuple(pk_values)

                if pk_tuple in seen_pk_tuples:
                    # Format the duplicate key for the error message
                    if len(table_info.primary_keys) == 1:
                        pk_desc = f"'{table_info.primary_keys[0]}' = '{pk_values[0]}'"
                    else:
                        pk_desc = ", ".join(
                            f"{k}={v}" for k, v in zip(table_info.primary_keys, pk_values)
                        )
                    errors.append(
                        ValidationError(
                            file=csv_path.name,
                            error_type="DUPLICATE_PK",
                            message=f"Duplicate primary key ({pk_desc}) - must be unique",
                            row=row_num,
                            column=table_info.primary_keys[0],
                        )
                    )
                else:
                    seen_pk_tuples.add(pk_tuple)

                # Register single-column PKs for FK validation
                if len(table_info.primary_keys) == 1:
                    self.id_registry[table_name].add(pk_values[0])

            # Check unique constraints
            for constraint in table_info.unique_constraints:
                constraint_key = tuple(constraint)
                unique_values: list[str] = []
                all_present = True

                for col_name in constraint:
                    if col_name in normalized_headers:
                        orig_header = normalized_headers[col_name]
                        val = row.get(orig_header, "").strip()
                        if val:
                            unique_values.append(val)
                        else:
                            # Empty values don't violate unique constraints
                            all_present = False
                            break
                    else:
                        all_present = False
                        break

                if all_present and unique_values:
                    value_tuple = tuple(unique_values)
                    if value_tuple in seen_unique_values[constraint_key]:
                        # Format the duplicate for error message
                        if len(constraint) == 1:
                            uc_desc = f"'{constraint[0]}' = '{unique_values[0]}'"
                        else:
                            uc_desc = ", ".join(
                                f"{k}={v}" for k, v in zip(constraint, unique_values)
                            )
                        errors.append(
                            ValidationError(
                                file=csv_path.name,
                                error_type="DUPLICATE_UNIQUE",
                                message=f"Duplicate value ({uc_desc}) - must be unique",
                                row=row_num,
                                column=constraint[0],
                            )
                        )
                    else:
                        seen_unique_values[constraint_key].add(value_tuple)

        success = len(errors) == 0
        return FileValidationResult(
            csv_path=csv_path,
            table_name=table_name,
            success=success,
            errors=errors,
            row_count=len(rows),
            warnings=warnings,
        )

    def _validate_row(
        self,
        filename: str,
        row_num: int,
        row: dict[str, str],
        table_info: TableInfo,
        normalized_headers: dict[str, str],
    ) -> list[ValidationError]:
        """Validate a single row."""
        errors: list[ValidationError] = []

        for norm_col, orig_col in normalized_headers.items():
            if norm_col not in table_info.columns:
                continue

            col_info = table_info.columns[norm_col]
            raw_value = row.get(orig_col)
            value = raw_value.strip() if raw_value else ""

            # Allow empty values for nullable columns, columns with defaults, or primary keys
            if not value and not col_info.nullable and not col_info.has_default:
                if not col_info.is_primary_key:
                    errors.append(
                        ValidationError(
                            file=filename,
                            error_type="NULL_VALUE",
                            message=f"Required column '{norm_col}' is empty",
                            row=row_num,
                            column=orig_col,
                        )
                    )
                continue

            if not value:
                continue

            type_error = self._validate_type(value, col_info)
            if type_error:
                errors.append(
                    ValidationError(
                        file=filename,
                        error_type="TYPE_ERROR",
                        message=type_error,
                        row=row_num,
                        column=orig_col,
                    )
                )

            # Validate enum values if defined
            if col_info.enum_values and value not in col_info.enum_values:
                errors.append(
                    ValidationError(
                        file=filename,
                        error_type="INVALID_ENUM",
                        message=f"Invalid value '{value}'. Must be one of: {col_info.enum_values}",
                        row=row_num,
                        column=orig_col,
                    )
                )

        return errors

    def _validate_type(self, value: str, col_info: ColumnInfo) -> str | None:
        """Validate that a value can be converted to the expected type."""
        try:
            if col_info.python_type is int:
                int(value)
            elif col_info.python_type is float:
                float(value.replace(",", "").replace("$", ""))
            elif col_info.python_type is bool:
                if value.lower() not in ("true", "false", "1", "0", "yes", "no"):
                    return f"Invalid boolean: '{value}'"
            elif col_info.python_type is datetime:
                parsed = False
                # Normalize trailing Z (valid ISO 8601) to +00:00 before parsing
                norm_value = value
                if norm_value.endswith("Z"):
                    norm_value = norm_value[:-1] + "+00:00"
                for fmt in [
                    "%Y-%m-%d",
                    "%Y-%m-%d %H:%M:%S",
                    "%Y-%m-%dT%H:%M:%S",
                    "%Y-%m-%dT%H:%M:%S%z",
                    "%m/%d/%Y",
                ]:
                    try:
                        datetime.strptime(
                            norm_value.split("+")[0].split(".")[0], fmt.split("%z")[0]
                        )
                        parsed = True
                        break
                    except ValueError:
                        continue
                if not parsed:
                    return f"Invalid datetime: '{value}'"
        except ValueError as e:
            return f"Cannot convert '{value}' to {col_info.python_type.__name__}: {e}"

        return None

    def validate_foreign_keys(self, results: list[FileValidationResult]) -> list[ValidationError]:
        """Validate FK references across all CSVs."""
        errors: list[ValidationError] = []

        for result in results:
            # Skip files that couldn't be matched to a table
            # But DO validate FKs even if file has other errors (e.g., enum errors)
            if not result.table_name:
                continue

            table_info = self.schema.tables[result.table_name]

            for fk_col, fk_target in table_info.foreign_keys.items():
                target_table = fk_target.split(".")[0]

                headers, rows = self.read_csv(result.csv_path)
                normalized_headers = {self.matcher.normalize_header(h): h for h in headers}

                if fk_col not in normalized_headers:
                    continue

                orig_col = normalized_headers[fk_col]

                for row_num, row in enumerate(rows, start=2):
                    fk_value = row.get(orig_col, "").strip()
                    if fk_value and fk_value not in self.id_registry.get(target_table, set()):
                        errors.append(
                            ValidationError(
                                file=result.csv_path.name,
                                error_type="FK_VIOLATION",
                                message=(
                                    f"Foreign key '{fk_col}' value '{fk_value}' "
                                    f"not found in {target_table}"
                                ),
                                row=row_num,
                                column=orig_col,
                            )
                        )

        return errors


# =============================================================================
# CSV Importer
# =============================================================================


class CSVImporter:
    """Imports validated CSVs into the database."""

    def __init__(self, schema: SchemaIntrospector, matcher: CSVMatcher, engine: Any):
        self.schema = schema
        self.matcher = matcher
        self.engine = engine

    async def import_csv(self, csv_path: Path, table_name: str, conn: Any = None) -> dict[str, Any]:
        """Import a single CSV file into its matched table.

        Args:
            csv_path: Path to the CSV file
            table_name: Name of the target table
            conn: Optional existing connection to reuse (for PRAGMA persistence)
        """
        logger.info(f"Importing {csv_path.name} → {table_name}")

        with open(csv_path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            headers = list(reader.fieldnames or [])
            rows = list(reader)

        table_info = self.schema.tables[table_name]
        normalized_headers = {self.matcher.normalize_header(h): h for h in headers}

        inserted = 0

        async def do_import(connection: Any) -> int:
            nonlocal inserted
            from sqlalchemy import MetaData, Table

            # Reflect table metadata once before the loop
            metadata = MetaData()
            table = await connection.run_sync(
                lambda sync_conn: Table(table_name, metadata, autoload_with=sync_conn)
            )

            for row in rows:
                row_data: dict[str, Any] = {}
                for norm_col, col_info in table_info.columns.items():
                    if norm_col in normalized_headers:
                        orig_col = normalized_headers[norm_col]
                        raw_value = row.get(orig_col)
                        value = raw_value.strip() if raw_value else ""
                        if value:
                            row_data[norm_col] = self._convert_value(value, col_info)
                        elif col_info.nullable:
                            row_data[norm_col] = None
                        elif col_info.default_value is not None:
                            # Apply scalar default
                            row_data[norm_col] = col_info.default_value
                    elif col_info.default_value is not None:
                        # Column missing from CSV - apply scalar default
                        row_data[norm_col] = col_info.default_value

                if row_data:
                    stmt = insert(table).values(**row_data)
                    await connection.execute(stmt)
                    inserted += 1
            return inserted

        if conn is not None:
            await do_import(conn)
        else:
            async with self.engine.begin() as new_conn:
                await do_import(new_conn)

        return {
            "file": csv_path.name,
            "table": table_name,
            "rows_inserted": inserted,
        }

    def _convert_value(self, value: str, col_info: ColumnInfo) -> Any:
        """Convert string value to appropriate Python type."""
        if col_info.python_type is int:
            return int(value)
        elif col_info.python_type is float:
            return float(value.replace(",", "").replace("$", ""))
        elif col_info.python_type is bool:
            return value.lower() in ("true", "1", "yes")
        elif col_info.python_type is datetime:
            # Normalize trailing Z (valid ISO 8601) to +00:00 before parsing
            norm_value = value
            if norm_value.endswith("Z"):
                norm_value = norm_value[:-1] + "+00:00"
            for fmt in [
                "%Y-%m-%d",
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S",
                "%m/%d/%Y",
            ]:
                try:
                    return datetime.strptime(norm_value.split("+")[0].split(".")[0], fmt)
                except ValueError:
                    continue
            return value
        elif col_info.python_type is dict:
            import json

            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        else:
            return value


# =============================================================================
# Core Functions (used by CLI and REST API)
# =============================================================================


def to_schema_response(base: type[DeclarativeBase]) -> SchemaResponse:
    """
    Get the database schema for frontend consumption.

    Introspects SQLAlchemy models to extract full schema information including
    tables, columns, types, foreign key relationships, required columns,
    unique constraints, and topological import order.

    Args:
        base: SQLAlchemy declarative base with registered models

    Returns:
        SchemaResponse with complete schema information
    """
    introspector = SchemaIntrospector(base)

    tables = []
    for table_info in introspector.tables.values():
        columns = []
        for col in table_info.columns.values():
            columns.append(
                ColumnSchemaResponse(
                    name=col.name,
                    type=col.python_type.__name__,
                    nullable=col.nullable,
                    is_primary_key=col.is_primary_key,
                    is_foreign_key=col.is_foreign_key,
                    fk_target=col.fk_target,
                    required=col.name in table_info.required_columns,
                    enum_values=col.enum_values,
                    is_unique=col.is_unique,
                    date_after=col.date_after,
                )
            )

        tables.append(
            TableSchemaResponse(
                name=table_info.name,
                columns=columns,
                primary_keys=table_info.primary_keys,
                foreign_keys=table_info.foreign_keys,
                required_columns=table_info.required_columns,
                unique_constraints=table_info.unique_constraints,
            )
        )

    return SchemaResponse(
        tables=tables,
        import_order=introspector.get_topological_order(),
        table_count=len(tables),
    )


def validate_csvs(
    csv_dir: Path,
    base: type[DeclarativeBase],
) -> ValidationResult:
    """
    Validate all CSV files in a directory against the database schema.

    Does NOT import. Does NOT touch the database.

    Checks:
    - CSV headers match database tables
    - Required columns are present
    - Data types are valid (int, float, bool, datetime)
    - Primary keys are unique (including composite PKs)
    - Foreign key references are valid across files

    Args:
        csv_dir: Directory containing CSV files to validate
        base: SQLAlchemy declarative base with registered models

    Returns:
        ValidationResult with detailed per-file results and FK errors
    """
    schema = SchemaIntrospector(base)
    matcher = CSVMatcher(schema)
    validator = CSVValidator(schema, matcher)

    # Find CSV files (case-insensitive)
    csv_files = _find_csv_files(csv_dir)

    if not csv_files:
        return ValidationResult(
            success=False,
            files=[],
            fk_errors=[
                ValidationError(
                    file="(none)",
                    error_type="NO_CSV_FILES",
                    message="No CSV files found in the directory",
                )
            ],
            total_errors=1,
            schema=schema,
        )

    # Phase 1: Validate each file
    file_results: list[FileValidationResult] = []
    for csv_path in csv_files:
        result = validator.validate_csv(csv_path)
        file_results.append(result)

    # Phase 1.5: Check for duplicate table mappings
    # Multiple CSVs matching the same table is an error
    duplicate_errors: list[ValidationError] = []
    table_to_file: dict[str, FileValidationResult] = {}
    for f in file_results:
        if f.table_name:
            if f.table_name in table_to_file:
                existing = table_to_file[f.table_name]
                duplicate_errors.append(
                    ValidationError(
                        file=f.csv_path.name,
                        error_type="DUPLICATE_TABLE",
                        message=f"Multiple CSVs match table '{f.table_name}': "
                        f"'{existing.csv_path.name}' and '{f.csv_path.name}'. "
                        f"Remove one of these files.",
                    )
                )
            else:
                table_to_file[f.table_name] = f

    # Phase 2: Cross-file FK validation
    fk_errors = validator.validate_foreign_keys(file_results)

    # Combine duplicate errors with FK errors
    all_cross_file_errors = duplicate_errors + fk_errors

    total_errors = sum(len(f.errors) for f in file_results) + len(all_cross_file_errors)

    return ValidationResult(
        success=total_errors == 0,
        files=file_results,
        fk_errors=all_cross_file_errors,
        total_errors=total_errors,
        schema=schema,
    )


async def import_csvs(
    csv_dir: Path,
    engine: Any,
    base: type[DeclarativeBase],
    confirm_clear: bool = False,
) -> ImportResult:
    """
    Validate and import CSV files into the database.

    1. Runs full validation (same as validate_csvs)
    2. If validation fails, returns immediately with errors
    3. Checks for existing data - if found and not confirmed, returns warning
    4. If confirmed, clears existing data and imports in topological order
    5. Verifies FK integrity post-import, rolls back on violation

    Args:
        csv_dir: Directory containing CSV files to import
        engine: SQLAlchemy async engine for database connection
        base: SQLAlchemy declarative base with registered models
        confirm_clear: If True, clear existing data without prompting

    Returns:
        ImportResult which always includes the ValidationResult
    """
    # Step 1: Validate first
    validation = validate_csvs(csv_dir, base)

    if not validation.success:
        return ImportResult(
            success=False,
            validation=validation,
            files=[],
            total_rows_imported=0,
            error_message=f"Validation failed with {validation.total_errors} error(s)",
        )

    # Step 2: Check for duplicate table mappings
    table_to_file: dict[str, FileValidationResult] = {}
    for f in validation.files:
        if f.table_name:
            if f.table_name in table_to_file:
                existing = table_to_file[f.table_name]
                return ImportResult(
                    success=False,
                    validation=validation,
                    files=[],
                    total_rows_imported=0,
                    error_message=f"Multiple CSVs match table '{f.table_name}': "
                    f"{existing.csv_path.name} and {f.csv_path.name}",
                )
            table_to_file[f.table_name] = f

    # Step 3: Import in topological order
    schema = validation.schema
    matcher = CSVMatcher(schema)
    importer = CSVImporter(schema, matcher, engine)
    import_order = schema.get_topological_order()

    # Step 3a: Check if ANY tables in the database have data
    # We need to clear ALL tables to avoid FK violations, not just imported ones
    all_tables = list(schema.tables.keys())
    tables_with_data: list[str] = []
    async with engine.connect() as check_conn:
        for table_name in all_tables:
            result = await check_conn.execute(
                text(f"SELECT COUNT(*) FROM {table_name}")  # noqa: S608
            )
            count = result.scalar()
            if count and count > 0:
                tables_with_data.append(table_name)

    # If existing data found and not confirmed, return warning
    if tables_with_data and not confirm_clear:
        return ImportResult(
            success=False,
            validation=validation,
            files=[],
            total_rows_imported=0,
            needs_confirmation=True,
            existing_data_tables=tables_with_data,
            error_message=f"Found existing data in {len(tables_with_data)} table(s). "
            "Confirm to clear and replace.",
        )

    import_results: list[FileImportResult] = []
    tables_cleared: list[str] = []

    try:
        async with engine.connect() as conn:
            await conn.run_sync(lambda c: c.exec_driver_sql("PRAGMA foreign_keys = OFF"))

            try:
                async with conn.begin():
                    # Clear ALL tables in reverse topological order
                    for table_name in reversed(import_order):
                        if table_name in tables_with_data:
                            logger.info(f"Clearing existing data from {table_name}")
                            await conn.execute(text(f"DELETE FROM {table_name}"))  # noqa: S608
                            tables_cleared.append(table_name)

                    for table_name in import_order:
                        if table_name in table_to_file:
                            f = table_to_file[table_name]
                            result = await importer.import_csv(f.csv_path, table_name, conn)
                            import_results.append(
                                FileImportResult(
                                    file_name=result["file"],
                                    table_name=result["table"],
                                    rows_imported=result["rows_inserted"],
                                )
                            )

                    # Verify FK integrity before commit
                    fk_check = await conn.run_sync(
                        lambda c: c.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
                    )

                    if fk_check:
                        # Build error message
                        violations = [
                            f"Table '{row[0]}' row {row[1]}: missing reference in '{row[2]}'"
                            for row in fk_check[:10]
                        ]
                        raise RuntimeError(
                            "FK violations detected after import:\n" + "\n".join(violations)
                        )

                    # Commit happens automatically when exiting `async with conn.begin()`
            finally:
                # Always re-enable FK checks, even on error
                await conn.run_sync(lambda c: c.exec_driver_sql("PRAGMA foreign_keys = ON"))

        return ImportResult(
            success=True,
            validation=validation,
            files=import_results,
            total_rows_imported=sum(f.rows_imported for f in import_results),
            tables_cleared=tables_cleared if tables_cleared else None,
        )

    except RuntimeError as e:
        # Transaction rolled back automatically
        return ImportResult(
            success=False,
            validation=validation,
            files=[],
            total_rows_imported=0,
            error_message=str(e),
        )


# =============================================================================
# CLI Presentation Helpers
# =============================================================================


def _print_validation_result(result: ValidationResult) -> None:
    """CLI presentation for validation results."""
    logger.info("=" * 60)
    logger.info("VALIDATION RESULTS")
    logger.info("=" * 60)

    for f in result.files:
        if f.success:
            logger.success(f"  ✓ {f.csv_path.name}: {f.row_count} rows → {f.table_name}")
        else:
            logger.error(f"  ✗ {f.csv_path.name}: {len(f.errors)} error(s)")
            for err in f.errors[:5]:
                logger.error(f"    - {err.error_type}: {err.message}")

    if result.fk_errors:
        logger.info("")
        logger.info("FK VIOLATIONS:")
        for err in result.fk_errors[:10]:
            logger.warning(f"  - {err.file} row {err.row}: {err.message}")

    logger.info("")
    logger.info(
        f"Files: {len(result.files)} total, "
        f"{sum(1 for f in result.files if f.success)} valid, "
        f"{sum(1 for f in result.files if not f.success)} invalid"
    )
    logger.info(f"Total errors: {result.total_errors}")

    if result.success:
        logger.success("VALIDATION PASSED")
    else:
        logger.error("VALIDATION FAILED")


def _print_import_result(result: ImportResult) -> None:
    """CLI presentation for import results."""
    # First show validation
    _print_validation_result(result.validation)

    if not result.validation.success:
        return  # Already printed errors

    logger.info("")
    logger.info("=" * 60)
    logger.info("IMPORT RESULTS")
    logger.info("=" * 60)

    if result.success:
        for f in result.files:
            logger.success(f"  ✓ {f.file_name}: {f.rows_imported} rows → {f.table_name}")
        logger.info("")
        logger.success(f"IMPORT COMPLETE: {result.total_rows_imported} total rows")
    else:
        logger.error(f"IMPORT FAILED: {result.error_message}")


# =============================================================================
# Main (CLI Entry Point)
# =============================================================================


async def main() -> None:
    """CLI entry point for CSV validation and import.

    Note: When running via wrapper script, the wrapper sets up sys.path before calling this.
    """
    # Import Base from the models package (paths must be set up by caller)
    from db.models import Base  # type: ignore[import-not-found]

    parser = argparse.ArgumentParser(
        description="Schema-aware CSV import for Greenhouse",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dir", required=True, help="Directory containing CSV files")
    parser.add_argument("--db", help="Database path (default: <dir>/data.db)")
    parser.add_argument("--validate-only", action="store_true", help="Only validate, don't import")
    args = parser.parse_args()

    logger.remove()
    logger.add(
        sys.stderr,
        format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}",
        level="INFO",
    )

    csv_dir = Path(args.dir)
    if not csv_dir.exists():
        logger.error(f"Directory not found: {csv_dir}")
        sys.exit(1)

    db_path = Path(args.db) if args.db else csv_dir / "data.db"

    if args.validate_only:
        # Validate only - no database needed
        logger.info(f"Validating CSV files in {csv_dir}...")
        result = validate_csvs(csv_dir, Base)
        _print_validation_result(result)
        sys.exit(0 if result.success else 1)
    else:
        # Validate and import
        logger.info(f"Importing CSV files from {csv_dir} to {db_path}...")
        database_url = f"sqlite+aiosqlite:///{db_path}"
        engine = create_async_engine(database_url)

        # Create tables before importing
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            result = await import_csvs(csv_dir, engine, Base)
            _print_import_result(result)
            sys.exit(0 if result.success else 1)
        finally:
            await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
