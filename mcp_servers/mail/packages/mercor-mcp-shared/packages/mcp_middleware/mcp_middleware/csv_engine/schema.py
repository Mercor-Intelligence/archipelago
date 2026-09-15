"""Schema introspection for csv_engine.

Two complementary introspection mechanisms:

1. **Connection-based** (``get_table_schema``): async, reads a single table
   from a live database via ``sa_inspect(connection)``.  Used by the
   importer/exporter for runtime column filtering and format inference.

2. **ORM-based** (``SchemaIntrospector``): sync, reads *all* tables from
   SQLAlchemy ``DeclarativeBase`` mappers.  Provides richer metadata (FK
   targets, enum values, unique constraints, topological ordering) used by
   the validator and the ``/schema`` REST endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import polars as pl
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.orm import DeclarativeBase

# Columns that are NOT NULL in the schema but auto-managed by the engine
# (transform_flat adds these automatically), so they should never appear
# in the inferred "required" set.
_AUTO_MANAGED_COLUMNS = {"created_at", "updated_at"}


# ---------------------------------------------------------------------------
# Connection-based schema (single-table, async, for importer/exporter)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ColumnInfo:
    """Metadata for a single database column (connection-level)."""

    name: str
    type_name: str  # e.g. "VARCHAR(100)", "DATETIME", "NUMERIC(15,2)"
    nullable: bool
    has_default: bool  # server_default or column default is set
    is_primary_key: bool


@dataclass(frozen=True)
class TableSchema:
    """Cached schema metadata for a database table."""

    table_name: str
    columns: dict[str, ColumnInfo]

    @property
    def column_names(self) -> set[str]:
        """All column names in this table."""
        return set(self.columns)

    @property
    def required_columns(self) -> set[str]:
        """NOT NULL columns without defaults, excluding PKs and auto-managed columns.

        These are the columns a CSV import *must* provide (unless hooks fill them).
        """
        return {
            name
            for name, col in self.columns.items()
            if not col.nullable
            and not col.has_default
            and not col.is_primary_key
            and name not in _AUTO_MANAGED_COLUMNS
        }

    def infer_format(self, column_name: str) -> str | None:
        """Infer CSV format string from the column's SQL type.

        Returns "date", "decimal", "bool", or None.
        """
        col = self.columns.get(column_name)
        if not col:
            return None
        t = col.type_name.upper()
        if any(kw in t for kw in ("DATE", "TIME", "TIMESTAMP")):
            return "date"
        if any(kw in t for kw in ("NUMERIC", "DECIMAL", "FLOAT", "REAL", "DOUBLE")):
            return "decimal"
        if "BOOL" in t:
            return "bool"
        return None


def polars_overrides(schema: TableSchema) -> dict[str, pl.DataType]:
    """Map ORM columns to ``polars`` dtypes for ``pl.scan_csv(schema_overrides=…)``.

    Currently forces **every** known column to :data:`polars.Utf8`. This serves
    two ends at once:

    1. **Kills the bool-inference bug class.** Without overrides, ``polars``
       auto-infers dtypes from the first batch of cells. A string-typed ORM
       column whose CSV happens to contain only ``"True"`` / ``"False"`` values
       gets inferred as :data:`polars.Boolean`, after which downstream
       ``.str.*`` operations (and any consumer ``parse_bool`` that expects a
       string input) blow up. Pinning to :data:`polars.Utf8` makes those
       columns stay strings end-to-end.
    2. **Drop-in match for the legacy ``csv.DictReader`` contract.** Every
       value reaches the importer / directives / row hooks as ``str``, exactly
       as before. Consumers' :func:`parse_decimal` / :func:`parse_bool` /
       :func:`parse_date` call sites continue to receive strings — no
       behaviour change to retrofit downstream.

    The natural-dtype path (numeric ORM cols → :class:`polars.Int64` /
    :class:`polars.Float64`, datetime → :class:`polars.Date` /
    :class:`polars.Datetime`) is deferred to the follow-up that lands
    alongside Phase C ``column_transforms``. That is the point at which
    consumers actually exploit native dtypes (vectorised arithmetic,
    ``pl.col(col).pipe(transform)`` chains), so it is the right moment to
    introduce the small downstream-coercion changes the switch requires.

    Args:
        schema: Cached schema metadata for one table (see
            :func:`get_table_schema`).

    Returns:
        ``{column_name: polars.Utf8}`` for every column in ``schema.columns``.
        Pass directly to ``polars.scan_csv(schema_overrides=...)``.
    """
    return {name: pl.Utf8() for name in schema.columns}


_schema_cache: dict[tuple[str, int, str], TableSchema] = {}


def clear_schema_cache() -> None:
    """Clear the schema cache.  Useful in tests."""
    _schema_cache.clear()


async def get_table_schema(
    engine: AsyncEngine,
    table_name: str,
    conn: AsyncConnection | None = None,
) -> TableSchema:
    """Introspect a database table and return cached schema metadata.

    Uses SQLAlchemy's ``inspect()`` to read column definitions and
    primary-key constraints.  Results are cached per
    ``(str(engine.url), id(engine), table_name)`` for the lifetime of
    the engine.

    The composite key solves two independent problems:

    * **In-memory SQLite isolation** — ``sqlite:///:memory:`` engines all
      share the same URL string but each backs a *distinct* database.
      Including ``id(engine)`` gives every engine instance its own cache
      slot, preventing stale hits across tests.

    * **Post-GC address reuse** — ``id(engine)`` alone is an integer
      (memory address) that Python *can* reuse after an engine is
      garbage-collected.  Adding ``str(engine.url)`` ensures that a
      recycled address pointing at a *different* database gets a fresh
      lookup; a recycled address with the *same* URL is connecting to the
      same database, so the cached schema is still valid.

    When ``conn`` is provided, introspection runs on that existing
    connection instead of opening a new one.  Callers that already hold a
    connection (e.g. the single-connection export paths) must pass it so
    that in-memory SQLite databases — where each connection is a separate
    database — are introspected against the same data being exported.
    """
    cache_key = (str(engine.url), id(engine), table_name)
    cached = _schema_cache.get(cache_key)
    if cached is not None:
        return cached

    def _introspect(sync_conn: Any) -> tuple[list[dict], set[str]]:
        insp = sa_inspect(sync_conn)
        cols = insp.get_columns(table_name)
        pk = insp.get_pk_constraint(table_name)
        pk_cols = set(pk.get("constrained_columns", []))
        return cols, pk_cols

    if conn is not None:
        raw_columns, pk_columns = await conn.run_sync(_introspect)
    else:
        async with engine.connect() as new_conn:
            raw_columns, pk_columns = await new_conn.run_sync(_introspect)

    columns: dict[str, ColumnInfo] = {}
    for col in raw_columns:
        columns[col["name"]] = ColumnInfo(
            name=col["name"],
            type_name=str(col["type"]),
            nullable=col.get("nullable", True),
            has_default=col.get("default") is not None,
            is_primary_key=col["name"] in pk_columns,
        )

    schema = TableSchema(table_name=table_name, columns=columns)
    _schema_cache[cache_key] = schema
    return schema


# ---------------------------------------------------------------------------
# ORM-based schema (all tables, sync, for validator / /schema endpoint)
# ---------------------------------------------------------------------------


@dataclass
class OrmColumnInfo:
    """Full ORM-level metadata for a single column."""

    name: str
    python_type: type  # int, float, bool, datetime, dict, str
    nullable: bool
    is_primary_key: bool
    is_foreign_key: bool
    fk_target: str | None = None  # e.g. "accounts.id"
    has_default: bool = False
    default_value: Any = None  # scalar default from ORM model
    enum_values: list[str] | None = None  # from column info dict
    is_unique: bool = False
    date_after: str | None = None


@dataclass
class TableInfo:
    """Full ORM-level metadata for a database table."""

    name: str
    columns: dict[str, OrmColumnInfo]
    primary_keys: list[str]
    foreign_keys: dict[str, str]  # col_name -> "table.column"
    required_columns: list[str]
    unique_constraints: list[list[str]] = field(default_factory=list)


class SchemaIntrospector:
    """Introspects SQLAlchemy ORM models to extract full schema information.

    Unlike ``get_table_schema`` which uses a live database connection,
    this class reads ``DeclarativeBase`` mappers directly, providing
    richer metadata: FK targets, enum values from ``info`` dicts,
    unique constraints, scalar defaults, and topological ordering.
    """

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
        columns: dict[str, OrmColumnInfo] = {}
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
            default_value = None
            if col.default is not None and col.default.is_scalar:
                default_value = col.default.arg

            # Get enum values: first check column info dict, then SQLAlchemy Enum type
            enum_values = col.info.get("enum") if col.info else None
            if enum_values is None:
                from sqlalchemy import Enum as SaEnum

                if isinstance(col.type, SaEnum):
                    enum_values = list(col.type.enums)
            date_after = col.info.get("date_after") if col.info else None

            # Check for column-level unique constraint
            is_unique = col.unique is True
            if is_unique:
                single_unique_cols.add(col.name)

            col_info = OrmColumnInfo(
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
        """
        children: dict[str, set[str]] = {name: set() for name in self.tables}
        in_degree: dict[str, int] = {name: 0 for name in self.tables}

        for table_name, table_info in self.tables.items():
            parent_tables: set[str] = set()
            for fk_target in table_info.foreign_keys.values():
                parent_table = fk_target.split(".")[0]
                if parent_table in self.tables and parent_table != table_name:
                    parent_tables.add(parent_table)

            for parent_table in parent_tables:
                children[parent_table].add(table_name)
                in_degree[table_name] += 1

        queue = [name for name, degree in in_degree.items() if degree == 0]
        result: list[str] = []

        while queue:
            node = queue.pop(0)
            result.append(node)

            for child in children[node]:
                in_degree[child] -= 1
                if in_degree[child] == 0:
                    queue.append(child)

        if len(result) != len(self.tables):
            remaining = set(self.tables.keys()) - set(result)
            logger.warning(f"Circular FK dependencies detected, adding remaining: {remaining}")
            result.extend(sorted(remaining))

        return result


# ---------------------------------------------------------------------------
# Pydantic response models for /schema REST endpoint
# ---------------------------------------------------------------------------


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


def to_schema_response(base: type[DeclarativeBase]) -> SchemaResponse:
    """Build REST-consumable schema response from ORM models.

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
