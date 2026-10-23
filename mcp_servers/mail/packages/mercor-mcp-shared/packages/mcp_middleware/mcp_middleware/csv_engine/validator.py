"""Pre-import CSV validation engine.

Validates CSV files against the database schema without importing.
Checks: table matching, required columns, data types, enums,
PK uniqueness, unique constraint violations, FK references.

Also provides the full validate-then-import flow with confirmation
for clearing existing data.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy import MetaData, Table, insert, text
from sqlalchemy.orm import DeclarativeBase

from .readers import SourceInfo, read_csv, read_csv_headers_text
from .schema import OrmColumnInfo, SchemaIntrospector, TableInfo

# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ValidationError:
    """A single validation error."""

    file: str
    error_type: str  # READ_ERROR, NO_HEADERS, NO_TABLE_MATCH, AMBIGUOUS_MATCH,
    # MISSING_REQUIRED, NULL_VALUE, TYPE_ERROR, INVALID_ENUM,
    # DUPLICATE_PK, DUPLICATE_UNIQUE, FK_VIOLATION, DUPLICATE_TABLE,
    # NO_CSV_FILES
    message: str
    row: int | None = None
    column: str | None = None


@dataclass
class FileValidationResult:
    """Result of validating a single CSV file."""

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
    tables_cleared: list[str] | None = None
    needs_confirmation: bool = False
    existing_data_tables: list[str] | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def is_valid_csv_file(path: Path | str) -> bool:
    """Check if a path is a valid CSV file (case-insensitive, excludes macOS metadata).

    Args:
        path: File path to check

    Returns:
        True if the file has a .csv extension and is not macOS metadata
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


# ---------------------------------------------------------------------------
# CSV Matcher
# ---------------------------------------------------------------------------


class CSVMatcher:
    """Matches CSV files to database tables based on headers."""

    def __init__(
        self,
        schema: SchemaIntrospector,
        entity_schemas: dict[str, Any] | None = None,
    ):
        self.schema = schema
        self.entity_schemas = entity_schemas
        # Build a flat alias -> canonical dict from entity_schemas.
        # Support both dict-style and object-style (dataclass/Pydantic) schemas,
        # and tolerate a missing/None ``aliases`` attribute.
        self._alias_map: dict[str, str] = {}
        if entity_schemas:
            for es in entity_schemas.values():
                if isinstance(es, dict):
                    aliases = es.get("aliases", {})
                else:
                    aliases = getattr(es, "aliases", None) or {}
                for alias, canonical in (aliases or {}).items():
                    self._alias_map[self._normalize_raw(alias)] = canonical

    @staticmethod
    def _normalize_raw(header: str) -> str:
        """Normalize a header to snake_case (without alias resolution)."""
        s = header.strip()
        # camelCase / PascalCase splitting
        s = re.sub(r"([a-z])([A-Z])", r"\1_\2", s)
        s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", s)
        # Replace separators with underscores
        s = re.sub(r"[\s\-\.]+", "_", s)
        s = re.sub(r"[^a-z0-9_]", "", s.lower())
        return s

    def normalize_header(self, header: str) -> str:
        """Normalize a header name for comparison, resolving aliases."""
        normalized = self._normalize_raw(header)
        # Resolve via alias map if available
        return self._alias_map.get(normalized, normalized)

    def match_csv_to_table(
        self, headers: list[str], csv_filename: str | None = None
    ) -> tuple[str | None, float, list[str] | None]:
        """Find the best matching table for given CSV headers.

        Args:
            headers: List of CSV column headers
            csv_filename: Optional filename (without extension) for tiebreaking

        Returns:
            Tuple of (table_name, match_score, ambiguous_tables)
        """
        if not headers:
            return None, 0.0, None

        normalized_headers = {self.normalize_header(h) for h in headers}

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

        matches.sort(key=lambda x: x[1], reverse=True)

        best_score = matches[0][1]
        top_matches = [m for m in matches if m[1] == best_score]

        if len(top_matches) == 1:
            return top_matches[0][0], top_matches[0][1], None

        # Multiple matches — try exact filename tiebreaker
        if csv_filename:
            normalized_filename = self.normalize_header(csv_filename)
            for table_name, score in top_matches:
                if table_name == normalized_filename:
                    logger.debug(f"Filename tiebreaker: '{csv_filename}' -> '{table_name}'")
                    return table_name, score, None

        tied_tables = [t[0] for t in top_matches]
        logger.debug(f"Ambiguous match: {tied_tables}")
        return None, best_score, tied_tables


# ---------------------------------------------------------------------------
# CSV Validator
# ---------------------------------------------------------------------------


class CSVValidator:
    """Validates CSV files against the database schema."""

    def __init__(self, schema: SchemaIntrospector, matcher: CSVMatcher):
        self.schema = schema
        self.matcher = matcher
        self.id_registry: dict[str, set[str]] = defaultdict(set)

    def read_csv(self, csv_path: Path) -> tuple[list[str], list[dict[str, str]]]:
        """Read CSV file and return headers and rows (polars-backed)."""
        data = csv_path.read_bytes()
        info = SourceInfo(filename=csv_path.name)
        headers = read_csv_headers_text(data, info)
        rows = list(read_csv(data, info))
        return headers, rows  # type: ignore[return-value]

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
        seen_pk_tuples: set[tuple[str, ...]] = set()

        # Track unique constraint values to detect duplicates
        seen_unique_values: dict[tuple[str, ...], set[tuple[str, ...]]] = {
            tuple(constraint): set() for constraint in table_info.unique_constraints
        }

        for row_num, row in enumerate(rows, start=2):
            row_errors = self._validate_row(
                csv_path.name, row_num, row, table_info, normalized_headers
            )
            errors.extend(row_errors)

            # Build composite PK tuple for this row
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

            if pk_columns_present and pk_values:
                pk_tuple = tuple(pk_values)

                if pk_tuple in seen_pk_tuples:
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
                            all_present = False
                            break
                    else:
                        all_present = False
                        break

                if all_present and unique_values:
                    value_tuple = tuple(unique_values)
                    if value_tuple in seen_unique_values[constraint_key]:
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

            # Allow empty values for nullable columns, columns with defaults, or PKs
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

    def _validate_type(self, value: str, col_info: OrmColumnInfo) -> str | None:
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


# ---------------------------------------------------------------------------
# CSV Importer (for validated data)
# ---------------------------------------------------------------------------


class CSVImporter:
    """Imports validated CSVs into the database with type conversion and defaults."""

    def __init__(self, schema: SchemaIntrospector, matcher: CSVMatcher, engine: Any):
        self.schema = schema
        self.matcher = matcher
        self.engine = engine

    async def import_csv(self, csv_path: Path, table_name: str, conn: Any = None) -> dict[str, Any]:
        """Import a single CSV file into its matched table.

        Args:
            csv_path: Path to the CSV file
            table_name: Name of the target table
            conn: Optional existing connection to reuse
        """
        logger.info(f"Importing {csv_path.name} → {table_name}")

        # polars-backed read: same all-strings contract as the legacy
        # csv.DictReader, no field-size-limit gotchas.
        data = csv_path.read_bytes()
        info = SourceInfo(filename=csv_path.name)
        headers = read_csv_headers_text(data, info)
        rows = list(read_csv(data, info))

        table_info = self.schema.tables[table_name]
        normalized_headers = {self.matcher.normalize_header(h): h for h in headers}

        inserted = 0

        async def do_import(connection: Any) -> int:
            nonlocal inserted

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
                            row_data[norm_col] = col_info.default_value
                    elif col_info.default_value is not None:
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

    def _convert_value(self, value: str, col_info: OrmColumnInfo) -> Any:
        """Convert string value to appropriate Python type."""
        if col_info.python_type is int:
            return int(value)
        elif col_info.python_type is float:
            return float(value.replace(",", "").replace("$", ""))
        elif col_info.python_type is bool:
            return value.lower() in ("true", "1", "yes")
        elif col_info.python_type is datetime:
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
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        else:
            return value


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_csvs(
    csv_dir: Path,
    base: type[DeclarativeBase],
    entity_schemas: dict[str, Any] | None = None,
) -> ValidationResult:
    """Validate all CSV files in directory against schema. Does NOT import.

    Checks:
    - CSV headers match database tables
    - Required columns are present
    - Data types are valid (int, float, bool, datetime)
    - Primary keys are unique (including composite PKs)
    - Unique constraints are not violated
    - Foreign key references are valid across files

    Args:
        csv_dir: Directory containing CSV files to validate
        base: SQLAlchemy declarative base with registered models
        entity_schemas: Optional dict of entity schemas for alias resolution

    Returns:
        ValidationResult with detailed per-file results and FK errors
    """
    schema = SchemaIntrospector(base)
    matcher = CSVMatcher(schema, entity_schemas=entity_schemas)
    validator = CSVValidator(schema, matcher)

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
    entity_schemas: dict[str, Any] | None = None,
) -> ImportResult:
    """Validate and import CSV files into the database.

    1. Runs full validation (same as validate_csvs)
    2. If validation fails, returns immediately with errors
    3. Checks for existing data — if found and not confirmed, returns warning
    4. If confirmed, clears existing data and imports in topological order
    5. Verifies FK integrity post-import, rolls back on violation

    Args:
        csv_dir: Directory containing CSV files to import
        engine: SQLAlchemy async engine
        base: SQLAlchemy declarative base with registered models
        confirm_clear: If True, clear existing data without prompting
        entity_schemas: Optional dict of entity schemas for alias resolution

    Returns:
        ImportResult which always includes the ValidationResult
    """
    # Step 1: Validate first
    validation = validate_csvs(csv_dir, base, entity_schemas=entity_schemas)

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
    matcher = CSVMatcher(schema, entity_schemas=entity_schemas)
    importer = CSVImporter(schema, matcher, engine)
    import_order = schema.get_topological_order()

    # Step 3a: Check if ANY tables have existing data
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
        async with engine.begin() as conn:
            # Disable FK checks during import
            await conn.run_sync(lambda c: c.exec_driver_sql("PRAGMA foreign_keys = OFF"))

            try:
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
            finally:
                await conn.run_sync(lambda c: c.exec_driver_sql("PRAGMA foreign_keys = ON"))

            # Step 4: Verify FK integrity before commit
            fk_check = await conn.run_sync(
                lambda c: c.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            )

            if fk_check:
                violations = [
                    f"Table '{row[0]}' row {row[1]}: missing reference in '{row[2]}'"
                    for row in fk_check[:10]
                ]
                raise RuntimeError("FK violations detected after import:\n" + "\n".join(violations))

        return ImportResult(
            success=True,
            validation=validation,
            files=import_results,
            total_rows_imported=sum(f.rows_imported for f in import_results),
            tables_cleared=tables_cleared if tables_cleared else None,
        )

    except RuntimeError as e:
        return ImportResult(
            success=False,
            validation=validation,
            files=[],
            total_rows_imported=0,
            error_message=str(e),
        )
