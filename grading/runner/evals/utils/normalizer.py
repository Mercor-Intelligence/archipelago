"""Normalizer for volatile fields in golden file comparison.

Handles timestamps, UUIDs, random strings, and other volatile values
that would cause hash mismatches even when semantic content matches.

Supports:
- JSON files: Structured field-name based normalization
- SQL dump files (.sql): Parses INSERT statements and normalizes volatile columns
- SQLite databases (.db, .sqlite, .sqlite3): Dumps to SQL and normalizes
- Jupyter notebooks (.ipynb): Clears execution state, preserves source code
- DICOM medical images (.dcm, .dicom): Normalizes PHI/timestamps, hashes pixel data
- Text files: Regex pattern-based normalization
"""

import hashlib
import io
import json
import re
import sqlite3
import tempfile
from collections.abc import Callable
from typing import Any

import sqlglot
from loguru import logger
from sqlglot import exp
from sqlglot.errors import ParseError

# Type alias for the normalizer function signature
NormalizerFunc = Callable[[bytes, str], bytes]

# Type alias for JSON values (inherently dynamic)
JsonValue = dict[str, Any] | list[Any] | str | int | float | bool | None

# Default patterns for common volatile fields in TEXT files
# These are applied to non-JSON content; JSON uses field-name normalization instead
# Patterns are intentionally conservative to avoid false positives
DEFAULT_PATTERNS: list[tuple[str, str]] = [
    # ISO 8601 timestamps (2024-03-18T12:34:56Z, 2024-03-18T12:34:56.789Z, etc.)
    (
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?",
        "<TIMESTAMP>",
    ),
    # UUIDs (v4 and other versions) - highly specific pattern
    (
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        "<UUID>",
    ),
    # MongoDB ObjectIDs - exactly 24 hex chars (not more, to avoid matching SHA hashes)
    (r"\b[0-9a-fA-F]{24}\b", "<OBJECT_ID>"),
]

# Fields commonly containing volatile values in JSON/YAML
# These field names (at any nesting level) will have their values normalized
DEFAULT_VOLATILE_FIELDS: set[str] = {
    "created_at",
    "createdAt",
    "updated_at",
    "updatedAt",
    "modified_at",
    "modifiedAt",
    "timestamp",
    "time",
    "date",
    "id",
    "uuid",
    "session_id",
    "sessionId",
    "request_id",
    "requestId",
    "trace_id",
    "traceId",
    "correlation_id",
    "correlationId",
    "token",
    "access_token",
    "refresh_token",
    "nonce",
    "etag",
    "version",
    "revision",
    "last_modified",
    "lastModified",
}

# SQL column names that typically contain volatile values
# Used for normalizing INSERT statements in SQL dumps
# Case-insensitive matching is applied
SQL_VOLATILE_COLUMNS: set[str] = {
    # Primary keys and identifiers
    "id",
    "uuid",
    "guid",
    "_id",
    "pk",
    # Timestamps
    "created_at",
    "updated_at",
    "modified_at",
    "deleted_at",
    "created",
    "modified",
    "creation",
    "timestamp",
    "time",
    "date",
    # Session/auth
    "session_id",
    "token",
    "access_token",
    "refresh_token",
    "nonce",
    "salt",
    # Version tracking
    "version",
    "revision",
    "etag",
    # Audit fields
    "created_by",
    "updated_by",
    "modified_by",
    "owner",
    # Password/hash fields (non-deterministic due to random salts)
    "password",
    "password_hash",
    "hash",
    # Auto-increment fields often named
    "rowid",
    "idx",
}

# File extensions that should be treated as binary (skip normalization)
# Note: .db, .sqlite, .sqlite3 are NOT in this list - they are normalized via SQL dump
BINARY_EXTENSIONS: set[str] = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".ico",
    ".webp",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".mp3",
    ".mp4",
    ".wav",
    ".avi",
    ".mov",
    ".webm",
    ".flv",
    ".mkv",
    ".wmv",
}

# SQLite database extensions that should be dumped to SQL and normalized
SQLITE_EXTENSIONS: set[str] = {
    ".db",
    ".sqlite",
    ".sqlite3",
}

# Jupyter notebook extensions
JUPYTER_EXTENSIONS: set[str] = {".ipynb"}

# DICOM medical imaging extensions
DICOM_EXTENSIONS: set[str] = {".dcm", ".dicom"}

# DICOM tags that contain volatile/patient-specific information
# These are normalized to enable semantic comparison
DICOM_VOLATILE_TAGS: set[str] = {
    # Instance UIDs (regenerated per export)
    "SOPInstanceUID",
    "StudyInstanceUID",
    "SeriesInstanceUID",
    "FrameOfReferenceUID",
    "MediaStorageSOPInstanceUID",
    # Timestamps (vary per acquisition/export)
    "StudyDate",
    "SeriesDate",
    "AcquisitionDate",
    "ContentDate",
    "InstanceCreationDate",
    "StudyTime",
    "SeriesTime",
    "AcquisitionTime",
    "ContentTime",
    "InstanceCreationTime",
    # Patient identifying information (PHI)
    "PatientID",
    "PatientName",
    "PatientBirthDate",
    "PatientSex",
    "PatientAge",
    # Operator/institution info
    "OperatorsName",
    "InstitutionName",
    "InstitutionalDepartmentName",
    "ReferringPhysicianName",
    "PerformingPhysicianName",
    # Accession and study identifiers
    "AccessionNumber",
    "StudyID",
    # Device identifiers
    "DeviceSerialNumber",
    "StationName",
}


def _normalize_json_value(
    value: JsonValue,
    volatile_fields: set[str],
    current_key: str | None = None,
) -> JsonValue:
    """Recursively normalize JSON values, replacing volatile fields with placeholders."""
    if current_key and current_key.lower() in {f.lower() for f in volatile_fields}:
        # This field is volatile - replace with placeholder based on type
        if isinstance(value, str):
            return "<NORMALIZED>"
        elif isinstance(value, int | float):
            return 0
        else:
            return "<NORMALIZED>"

    if isinstance(value, dict):
        return {
            k: _normalize_json_value(v, volatile_fields, k) for k, v in value.items()
        }
    elif isinstance(value, list):
        return [_normalize_json_value(v, volatile_fields, None) for v in value]
    else:
        return value


def _apply_patterns(content: str, patterns: list[tuple[str, str]]) -> str:
    """Apply regex patterns to normalize volatile values in text content."""
    for pattern, replacement in patterns:
        try:
            content = re.sub(pattern, replacement, content)
        except re.error as e:
            logger.warning(f"Invalid normalization pattern '{pattern}': {e}")
    return content


def _detect_sql_dialect(sql_content: str) -> str:
    """Detect SQL dialect from content.

    Returns: "mysql", "postgres", or "sqlite"
    """
    sql_lower = sql_content.lower()
    # MySQL/MariaDB: backticks, ENGINE=, AUTO_INCREMENT
    if "`" in sql_content or "engine=" in sql_lower or "auto_increment" in sql_lower:
        return "mysql"
    # PostgreSQL: SERIAL (as type), :: casts
    if re.search(r"\bserial\b", sql_lower) or "::" in sql_content:
        return "postgres"
    return "sqlite"


def _normalize_insert_values(
    insert_expr: exp.Insert,
    volatile_columns: set[str],
    table_columns: dict[str, list[str]] | None = None,
) -> exp.Insert:
    """Normalize volatile column values in an INSERT statement.

    Replaces values in volatile columns with '<NORMALIZED>' placeholder.

    Args:
        insert_expr: A sqlglot INSERT expression
        volatile_columns: Set of column names (case-insensitive) to normalize
        table_columns: Optional map of table_name -> column_names for INSERT without
                       explicit columns (from CREATE TABLE statements)

    Returns:
        Modified INSERT expression with volatile values replaced
    """
    # Get column names from INSERT
    # In sqlglot, INSERT INTO table (col1, col2) creates a Schema with columns in expressions
    table = insert_expr.args.get("this")
    column_names: list[str] = []
    table_name: str = ""

    if table is not None:
        if isinstance(table, exp.Schema):
            # Schema contains table name and column list (explicit columns)
            for col in table.expressions:
                if hasattr(col, "name"):
                    column_names.append(str(col.name).lower())
            # Get table name from the nested Table
            inner_table = table.args.get("this")
            if inner_table and hasattr(inner_table, "name"):
                table_name = str(inner_table.name).lower()
        elif isinstance(table, exp.Table):
            # Just a table reference, no explicit columns (e.g., INSERT INTO tbl VALUES)
            if hasattr(table, "name"):
                table_name = str(table.name).lower()
            # Try to get column names from table_columns map
            if table_columns and table_name in table_columns:
                column_names = table_columns[table_name]

    if not column_names:
        # No explicit columns and no table definition available
        return insert_expr

    volatile_indices = {
        i for i, name in enumerate(column_names) if name in volatile_columns
    }

    if not volatile_indices:
        return insert_expr

    # Get the VALUES expression
    values_expr = insert_expr.args.get("expression")
    if not values_expr:
        return insert_expr

    # Handle VALUES (...), (...), ... format
    if isinstance(values_expr, exp.Values):
        for tuple_expr in values_expr.expressions:
            if isinstance(tuple_expr, exp.Tuple):
                for i in volatile_indices:
                    if i < len(tuple_expr.expressions):
                        # Replace with normalized placeholder literal
                        tuple_expr.expressions[i] = exp.Literal.string("<NORMALIZED>")
    # Handle single tuple VALUES (...)
    elif isinstance(values_expr, exp.Tuple):
        for i in volatile_indices:
            if i < len(values_expr.expressions):
                values_expr.expressions[i] = exp.Literal.string("<NORMALIZED>")

    return insert_expr


def _extract_table_columns(create_expr: exp.Create) -> tuple[str, list[str]] | None:
    """Extract table name and column names from a CREATE TABLE statement.

    Returns:
        Tuple of (table_name, column_names) or None if not a CREATE TABLE
    """
    # Check if this is a CREATE TABLE
    if not isinstance(create_expr, exp.Create):
        return None

    kind = create_expr.args.get("kind")
    if kind != "TABLE":
        return None

    # Get table name
    table_expr = create_expr.args.get("this")
    if not table_expr:
        return None

    table_name: str = ""
    if isinstance(table_expr, exp.Schema):
        inner_table = table_expr.args.get("this")
        if inner_table and hasattr(inner_table, "name"):
            table_name = str(inner_table.name).lower()
    elif hasattr(table_expr, "name"):
        table_name = str(table_expr.name).lower()

    if not table_name:
        return None

    # Get column definitions from the Schema's expressions
    column_names: list[str] = []
    schema = create_expr.args.get("this")
    if isinstance(schema, exp.Schema):
        for col_def in schema.expressions:
            if isinstance(col_def, exp.ColumnDef):
                col_name = col_def.args.get("this")
                if col_name and hasattr(col_name, "name"):
                    column_names.append(str(col_name.name).lower())

    return (table_name, column_names) if column_names else None


def _get_insert_sort_key(insert_expr: exp.Insert) -> tuple[str, str]:
    """Generate a sort key for an INSERT statement.

    Sort by: (table_name, normalized_values_string)
    This ensures consistent ordering even when agents insert rows in different order.
    """
    table_name = ""
    table = insert_expr.args.get("this")
    if table and hasattr(table, "name"):
        table_name = table.name

    # Use the SQL representation of values as secondary sort key
    values_expr = insert_expr.args.get("expression")
    values_str = values_expr.sql() if values_expr else ""

    return (table_name, values_str)


def _normalize_sql_dump(
    sql_content: str,
    volatile_columns: set[str] | None = None,
) -> str:
    """Normalize a SQL dump for golden file comparison.

    Parses SQL, normalizes volatile column values in INSERT statements,
    and sorts INSERT statements for consistent ordering.

    Args:
        sql_content: Raw SQL dump content
        volatile_columns: Column names to normalize (defaults to SQL_VOLATILE_COLUMNS)

    Returns:
        Normalized SQL string
    """
    if volatile_columns is None:
        volatile_columns = {col.lower() for col in SQL_VOLATILE_COLUMNS}
    else:
        volatile_columns = {col.lower() for col in volatile_columns}

    # Remove PRAGMA statements (SQLite-specific, often contain path info)
    sql_content = re.sub(
        r"^\s*PRAGMA\s+[^;]+;", "", sql_content, flags=re.MULTILINE | re.IGNORECASE
    )

    # Detect dialect and parse
    dialect = _detect_sql_dialect(sql_content)
    try:
        expressions = sqlglot.parse(sql_content, dialect=dialect)
    except ParseError as e:
        logger.warning(f"Failed to parse SQL dump as {dialect}: {e}")
        # Fall back to returning original content
        return sql_content

    # First pass: Extract table column definitions for INSERT without explicit columns
    table_columns: dict[str, list[str]] = {}
    for expr in expressions:
        if expr is None:
            continue
        if isinstance(expr, exp.Create):
            result = _extract_table_columns(expr)
            if result:
                table_name, columns = result
                table_columns[table_name] = columns

    # Statement types to filter out (transaction control adds noise, not needed for comparison)
    transaction_types = (exp.Transaction, exp.Commit, exp.Rollback)

    # Separate INSERT statements from other statements for sorting
    # Note: sqlglot.parse returns list[Expr | None], we store non-INSERT as generic expressions
    inserts: list[exp.Insert] = []
    other_statements: list[exp.Expression] = []

    for expr in expressions:
        if expr is None:
            continue
        # Skip transaction control statements (BEGIN, COMMIT, ROLLBACK)
        if isinstance(expr, transaction_types):
            continue
        if isinstance(expr, exp.Insert):
            # Normalize volatile columns in INSERT, using table_columns for implicit columns
            normalized_insert = _normalize_insert_values(
                expr, volatile_columns, table_columns
            )
            inserts.append(normalized_insert)
        elif isinstance(expr, exp.Expression):
            other_statements.append(expr)

    # Sort INSERT statements for consistent ordering
    inserts.sort(key=_get_insert_sort_key)

    # Reconstruct SQL: other statements first, then sorted INSERTs
    # Use sqlite dialect for output to ensure consistent format
    output_parts = []

    for stmt in other_statements:
        try:
            output_parts.append(stmt.sql(dialect="sqlite") + ";")
        except Exception as e:
            logger.debug(f"Failed to serialize statement: {e}")

    for insert in inserts:
        try:
            output_parts.append(insert.sql(dialect="sqlite") + ";")
        except Exception as e:
            logger.debug(f"Failed to serialize INSERT: {e}")

    return "\n".join(output_parts)


def _normalize_sqlite_db(
    content: bytes,
    volatile_columns: set[str] | None = None,
) -> bytes:
    """Normalize a SQLite database file for golden file comparison.

    Dumps the database to SQL and applies SQL dump normalization.

    Args:
        content: Raw SQLite database file bytes
        volatile_columns: Column names to normalize (defaults to SQL_VOLATILE_COLUMNS)

    Returns:
        Normalized SQL dump bytes
    """
    # Write to temp file (SQLite needs file path)
    with tempfile.NamedTemporaryFile(suffix=".db", delete=True) as temp_file:
        temp_file.write(content)
        temp_file.flush()

        try:
            with sqlite3.connect(temp_file.name) as conn:
                # Dump database to SQL
                sql_lines = list(conn.iterdump())
        except sqlite3.Error as e:
            logger.warning(f"Failed to read SQLite database: {e}")
            return content

    sql_content = "\n".join(sql_lines)
    normalized = _normalize_sql_dump(sql_content, volatile_columns)
    return normalized.encode("utf-8")


def _normalize_jupyter_notebook(content: bytes) -> bytes:
    """Normalize Jupyter notebook for comparison.

    Clears volatile fields that change on each execution:
    - execution_count: Increments every cell run
    - outputs: Can contain timestamps, memory addresses, random values
    - cell metadata: Kernel-specific info
    - top-level metadata: Kernel versions, environment info

    Preserves: cell source code, cell types, nbformat version
    """
    try:
        notebook = json.loads(content.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.warning(f"Failed to parse Jupyter notebook: {e}")
        return content

    # Normalize top-level metadata (keep only language name for context)
    if "metadata" in notebook:
        lang = notebook.get("metadata", {}).get("language_info", {}).get("name")
        notebook["metadata"] = {"language_info": {"name": lang}} if lang else {}

    # Normalize each cell
    for cell in notebook.get("cells", []):
        # Clear cell metadata for all cell types
        cell["metadata"] = {}
        # Normalize cell ID if present (Jupyter 4.5+ cell IDs)
        if "id" in cell:
            cell["id"] = "<NORMALIZED>"
        # Only code cells have execution_count and outputs
        if cell.get("cell_type") == "code":
            cell["execution_count"] = None
            cell["outputs"] = []

    return json.dumps(notebook, sort_keys=True, indent=2).encode("utf-8")


def _normalize_dicom(content: bytes) -> bytes:
    """Normalize DICOM medical imaging file for comparison.

    Extracts metadata to JSON format, normalizing volatile fields.
    Pixel data is hashed (SHA-256) to enable image content verification
    without including the full binary data in the comparison.

    Requires pydicom library.
    """
    try:
        import pydicom
    except ImportError:
        logger.warning("pydicom not installed, skipping DICOM normalization")
        return content

    try:
        # Read full DICOM including pixel data for hashing
        ds = pydicom.dcmread(io.BytesIO(content))
    except Exception as e:
        logger.warning(f"Failed to read DICOM file: {e}")
        return content

    metadata: dict[str, str] = {}

    # Hash pixel data if present for image content verification
    if "PixelData" in ds:
        try:
            pixel_bytes = ds.PixelData
            if isinstance(pixel_bytes, bytes):
                pixel_hash = hashlib.sha256(pixel_bytes).hexdigest()
                metadata["_pixel_data_hash"] = pixel_hash
        except Exception as e:
            logger.debug(f"Could not hash pixel data: {e}")
            metadata["_pixel_data_hash"] = "<UNREADABLE>"

    for elem in ds:
        # Skip binary data elements (already handled pixel data above)
        # OB=Other Byte, OW=Other Word, UN=Unknown, SQ=Sequence (nested datasets)
        if elem.keyword == "PixelData" or elem.VR in ("OB", "OW", "UN", "SQ"):
            continue

        keyword = elem.keyword or f"Tag{elem.tag.group:04X},{elem.tag.element:04X}"
        if keyword in DICOM_VOLATILE_TAGS:
            metadata[keyword] = "<NORMALIZED>"
        else:
            # Convert value to string representation
            try:
                metadata[keyword] = str(elem.value)
            except Exception:
                metadata[keyword] = "<UNREADABLE>"

    return json.dumps(metadata, sort_keys=True, indent=2).encode("utf-8")


def normalize_content(
    content: bytes,
    filename: str,
    patterns: list[tuple[str, str]] | None = None,
    volatile_fields: set[str] | None = None,
    skip_binary: bool = True,
) -> bytes:
    """
    Normalize file content to handle volatile fields for comparison.

    Args:
        content: Raw file bytes
        filename: Name of the file (used for extension detection)
        patterns: Regex patterns to apply (defaults to DEFAULT_PATTERNS)
        volatile_fields: Field names to normalize in JSON/YAML (defaults to DEFAULT_VOLATILE_FIELDS)
        skip_binary: If True, skip normalization for binary files

    Returns:
        Normalized content bytes
    """
    if patterns is None:
        patterns = DEFAULT_PATTERNS
    if volatile_fields is None:
        volatile_fields = DEFAULT_VOLATILE_FIELDS

    # Check for binary files (excluding SQLite which we handle specially)
    ext = filename[filename.rfind(".") :].lower() if "." in filename else ""
    if skip_binary and ext in BINARY_EXTENSIONS:
        return content

    # SQLite database files: dump to SQL and normalize
    if ext in SQLITE_EXTENSIONS:
        try:
            return _normalize_sqlite_db(content)
        except Exception as e:
            logger.warning(f"Failed to normalize SQLite file {filename}: {e}")
            return content

    # SQL dump files: parse and normalize
    if ext == ".sql":
        try:
            text = content.decode("utf-8")
            normalized = _normalize_sql_dump(text)
            return normalized.encode("utf-8")
        except UnicodeDecodeError:
            logger.warning(f"Could not decode {filename} as UTF-8")
            return content
        except Exception as e:
            logger.warning(f"Failed to normalize SQL file {filename}: {e}")
            return content

    # Jupyter notebook files: normalize volatile execution state
    if ext in JUPYTER_EXTENSIONS:
        try:
            return _normalize_jupyter_notebook(content)
        except Exception as e:
            logger.warning(f"Failed to normalize Jupyter notebook {filename}: {e}")
            return content

    # DICOM medical imaging files: extract metadata to JSON
    if ext in DICOM_EXTENSIONS:
        try:
            return _normalize_dicom(content)
        except Exception as e:
            logger.warning(f"Failed to normalize DICOM file {filename}: {e}")
            return content

    # Try to decode as text
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        # Binary content, return as-is
        return content

    # JSON/YAML files: use structured normalization
    if ext in {".json", ".yaml", ".yml"}:
        try:
            if ext == ".json":
                data = json.loads(text)
                normalized = _normalize_json_value(data, volatile_fields)
                # Use sorted keys for consistent ordering
                return json.dumps(normalized, sort_keys=True, indent=2).encode("utf-8")
            else:
                # For YAML, fall through to pattern-based normalization
                # Could add ruamel.yaml support if needed
                pass
        except (json.JSONDecodeError, ValueError) as e:
            logger.debug(f"Could not parse {filename} as JSON: {e}")
            # Fall through to pattern-based normalization

    # Apply regex patterns for text content
    normalized_text = _apply_patterns(text, patterns)
    return normalized_text.encode("utf-8")


def create_normalizer(
    additional_patterns: list[tuple[str, str]] | None = None,
    additional_volatile_fields: list[str] | None = None,
    disable_defaults: bool = False,
) -> NormalizerFunc:
    """
    Create a normalizer function with custom configuration.

    Args:
        additional_patterns: Extra regex patterns to add to defaults
        additional_volatile_fields: Extra field names to add to defaults
        disable_defaults: If True, only use the provided patterns/fields

    Returns:
        A function that normalizes content bytes
    """
    if disable_defaults:
        patterns = list(additional_patterns or [])
        fields = set(additional_volatile_fields or [])
    else:
        patterns = list(DEFAULT_PATTERNS)
        if additional_patterns:
            patterns.extend(additional_patterns)
        fields = set(DEFAULT_VOLATILE_FIELDS)
        if additional_volatile_fields:
            fields.update(additional_volatile_fields)

    def normalizer(content: bytes, filename: str) -> bytes:
        return normalize_content(
            content,
            filename,
            patterns=patterns,
            volatile_fields=fields,
        )

    return normalizer
