"""Pydantic models for Looker MCP server.

Models are organized by API surface area:
1. LookML Models & Explores (core discovery)
2. Queries (core execution)
3. Content Discovery (nice-to-have)

Input models implement APIConfigurable protocol for repository pattern.
"""

import json
from enum import Enum
from typing import Any

from mcp_schema import GeminiBaseModel as BaseModel
from pydantic import Field, field_validator

# =============================================================================
# Enums for discoverable options
# =============================================================================


class ChartType(str, Enum):
    """Supported chart types for visualization rendering.

    These chart types are used in vis_config to specify how query results
    should be visualized when rendering PNG or PDF outputs.
    """

    LOOKER_COLUMN = "looker_column"
    """Vertical bar chart - best for comparing categories"""

    LOOKER_BAR = "looker_bar"
    """Horizontal bar chart - best for long category labels"""

    LOOKER_LINE = "looker_line"
    """Line chart - best for trends over time"""

    LOOKER_PIE = "looker_pie"
    """Pie chart - best for showing proportions (max 10 slices)"""

    LOOKER_AREA = "looker_area"
    """Area chart - best for cumulative trends"""

    LOOKER_SCATTER = "looker_scatter"
    """Scatter plot - best for showing correlations"""

    SINGLE_VALUE = "single_value"
    """Single large number display - best for KPIs"""

    TABLE = "table"
    """Data table - best for detailed data inspection"""


class JoinType(str, Enum):
    """SQL join types for connecting views in an Explore."""

    LEFT_OUTER = "left_outer"
    """Left outer join - keeps all rows from left table"""

    INNER = "inner"
    """Inner join - only matching rows from both tables"""

    FULL_OUTER = "full_outer"
    """Full outer join - keeps all rows from both tables"""

    CROSS = "cross"
    """Cross join - cartesian product of both tables"""


class RelationshipType(str, Enum):
    """Cardinality relationship between joined views."""

    MANY_TO_ONE = "many_to_one"
    """Many rows in this view match one row in the joined view"""

    ONE_TO_MANY = "one_to_many"
    """One row in this view matches many rows in the joined view"""

    ONE_TO_ONE = "one_to_one"
    """One row in this view matches exactly one row in the joined view"""

    MANY_TO_MANY = "many_to_many"
    """Many rows can match many rows (requires bridge table)"""


class ViewType(str, Enum):
    """Type of view within an Explore."""

    BASE = "base"
    """Primary/base view of the Explore"""

    JOINED = "joined"
    """A view joined to the base view"""


class ContentType(str, Enum):
    """Types of content that can be searched."""

    LOOK = "look"
    """Saved Look (query + visualization)"""

    DASHBOARD = "dashboard"
    """Dashboard (collection of tiles)"""


class ExportFormat(str, Enum):
    """Export format for query results."""

    JSON = "json"
    """JSON format - structured data"""

    CSV = "csv"
    """CSV format - comma-separated values"""


class FieldType(str, Enum):
    """Data types for LookML fields."""

    STRING = "string"
    """Text/string values"""

    NUMBER = "number"
    """Numeric values (integers or decimals)"""

    DATE = "date"
    """Date values (without time)"""

    DATETIME = "datetime"
    """Date and time values"""

    TIME = "time"
    """Time-only values"""

    YESNO = "yesno"
    """Boolean yes/no values"""

    TIER = "tier"
    """Tiered/bucketed numeric values"""

    LOCATION = "location"
    """Geographic location values"""

    ZIPCODE = "zipcode"
    """ZIP/postal code values"""


class TileType(str, Enum):
    """Types of dashboard tiles."""

    LOOKER_COLUMN = "looker_column"
    """Vertical bar chart tile"""

    LOOKER_BAR = "looker_bar"
    """Horizontal bar chart tile"""

    LOOKER_LINE = "looker_line"
    """Line chart tile"""

    LOOKER_PIE = "looker_pie"
    """Pie chart tile"""

    LOOKER_AREA = "looker_area"
    """Area chart tile"""

    LOOKER_SCATTER = "looker_scatter"
    """Scatter plot tile"""

    SINGLE_VALUE = "single_value"
    """Single value/KPI tile"""

    TABLE = "table"
    """Data table tile"""

    TEXT = "text"
    """Text/markdown tile"""

    BUTTON = "button"
    """Button/action tile"""


class VisConfig(BaseModel):
    """Visualization configuration for chart rendering.

    Controls how query results are rendered as PNG charts or PDF reports.
    """

    model_config = {"extra": "allow"}  # Allow extra fields to pass through

    type: ChartType = Field(
        default=ChartType.LOOKER_COLUMN,
        description="Chart type to render. Options: "
        "'looker_column' (vertical bars - best for comparing categories), "
        "'looker_bar' (horizontal bars - best for long category labels), "
        "'looker_line' (line chart - best for trends over time), "
        "'looker_pie' (pie chart - best for proportions, max 10 slices recommended), "
        "'looker_area' (area chart - best for cumulative trends), "
        "'looker_scatter' (scatter plot - best for correlations between two measures), "
        "'single_value' (large number - best for KPIs and single metrics), "
        "'table' (data table - best for detailed inspection)",
    )


# =============================================================================
# Base Classes
# =============================================================================


class APIConfigurableBase(BaseModel):
    """Base class for models that implement APIConfigurable protocol.

    Provides default implementations of protocol methods.
    """

    def matches(self, lookup_key: dict[str, Any]) -> bool:
        """Check if this input model matches the given lookup key.

        Default implementation: matches if all fields in lookup_key equal
        the corresponding fields in this model. Empty lookup_key matches everything.

        Override this method if you need custom matching logic.

        Args:
            lookup_key: Dictionary of field names to values to match against

        Returns:
            True if this model matches the lookup criteria, False otherwise
        """
        # Empty lookup key matches everything (for list-all endpoints)
        if not lookup_key:
            return True

        # Check all fields in lookup_key match this model's fields
        for key, value in lookup_key.items():
            if not hasattr(self, key) or getattr(self, key) != value:
                return False

        return True

    @classmethod
    def create_repository(cls, response_class: type[BaseModel]):
        """Create repository for this model type.

        Creates offline repository (DuckDB) in offline mode, or live repository
        (Looker API) in online mode.

        Mode selection priority:
        1. Offline mode (if explicitly set or no credentials)
        2. Online mode (if credentials exist and are working)
        3. Fallback to offline (if online fails)

        Override this method to customize repository creation for specific modes.
        Call super().create_repository(response_class) to fall back to default behavior.

        Args:
            response_class: The response model class

        Returns:
            Repository instance configured for the current mode
        """
        # Import repository_factory to access settings (for test patchability)
        import repository_factory

        if repository_factory.settings.is_offline_mode():
            return repository_factory._create_mock_repository(cls, response_class)
        else:
            return repository_factory._create_live_repository(cls, response_class)


# =============================================================================
# 1. LookML Models & Explores (Core Discovery)
# =============================================================================


class LookMLModelRequest(APIConfigurableBase):
    """List all available LookML models.

    This is the entry point for discovering what data is available
    in the Looker instance. Each model represents a semantic layer
    with explores, dimensions, and measures.

    No parameters needed - lists all available models.
    """

    pass  # No input parameters needed

    @staticmethod
    def get_api_config() -> dict:
        """Get API configuration for this endpoint."""
        return {
            "url_template": "/lookml_models",
            "method": "GET",
            "endpoint": "lookml_models",
            "response_key": "models",  # Wrap array response in {"models": [...]}
        }

    def to_template_values(self) -> dict[str, str]:
        """Convert to template values."""
        return {}

    @staticmethod
    def tool_name() -> str:
        """Override default tool name."""
        return "list_lookml_models"


class LookmlModelNavExplore(BaseModel):
    """Explore metadata within a LookML model."""

    name: str = Field(..., description="Explore name")
    description: str | None = Field(None, description="Explore description")
    label: str | None = Field(None, description="UI-friendly label")
    hidden: bool = Field(False, description="Whether explore is hidden")
    group_label: str | None = Field(None, description="Navigation group label")
    is_bundled: bool = Field(False, description="Whether explore is bundled (pre-seeded) data")


class LookMLModel(BaseModel):
    """A single LookML model definition.

    Matches the official Looker API 4.0 response schema.
    See: https://docs.cloud.google.com/looker/docs/reference/looker-api/latest/methods/LookmlModel/all_lookml_models
    """

    name: str = Field(..., description="Model name", examples=["ecommerce"])
    project_name: str | None = Field(None, description="Project this model belongs to")
    label: str | None = Field(None, description="Human-readable label")
    explores: list[LookmlModelNavExplore] = Field(
        default_factory=list, description="List of explores with metadata"
    )
    allowed_db_connection_names: list[str] | None = Field(
        None, description="Database connections available to this model"
    )
    unlimited_db_connections: bool = Field(
        False, description="Whether model can use any database connection"
    )


class LookMLModelResponse(BaseModel):
    """Response containing all LookML models."""

    models: list[LookMLModel] = Field(..., description="Available LookML models")


class ExploreRequest(APIConfigurableBase):
    """Get detailed information about a specific Explore.

    This returns the complete semantic layer definition for an explore:
    - Available dimensions (groupable fields)
    - Available measures (aggregations)
    - Join relationships between tables
    - Field metadata (labels, types, descriptions)

    This is the primary tool for understanding:
    - "What fields are available?"
    - "How are these tables joined?"
    - "What can I query from this explore?"
    """

    model: str = Field(..., description="Model name", examples=["ecommerce"])
    explore: str = Field(..., description="Explore name", examples=["order_items"])

    @staticmethod
    def get_api_config() -> dict:
        """Get API configuration for this endpoint."""
        return {
            "url_template": "/lookml_models/{model}/explores/{explore}",
            "method": "GET",
            "endpoint": "explores",
        }

    def to_template_values(self) -> dict[str, str]:
        """Convert to template values for URL substitution."""
        return {"model": self.model, "explore": self.explore}


class LookMLField(BaseModel):
    """A dimension or measure in an Explore."""

    model_config = {"extra": "ignore"}  # Ignore extra fields from API

    name: str = Field(..., description="Field name", examples=["order_items.count"])
    label: str | None = Field(None, description="Human-readable label")
    type: FieldType | str = Field(
        ...,
        description="Field data type: string, number, date, datetime, time, yesno, tier, location",
    )
    description: str | None = Field(None, description="Field description")
    sql: str | None = Field(None, description="SQL expression (if available)")
    hidden: bool = Field(False, description="Whether field is hidden")
    view: str | None = Field(None, description="View this field belongs to")


class LookMLJoin(BaseModel):
    """A join relationship in an Explore."""

    name: str = Field(..., description="Joined view name")
    type: JoinType | str = Field(
        JoinType.LEFT_OUTER,
        description="SQL join type: left_outer, inner, full_outer, cross",
    )
    sql_on: str | None = Field(None, description="JOIN ON condition")
    relationship: RelationshipType | str = Field(
        RelationshipType.MANY_TO_ONE,
        description="Cardinality: many_to_one, one_to_many, one_to_one, many_to_many",
    )


class ExploreFields(BaseModel):
    """Fields container for an Explore."""

    model_config = {"extra": "ignore"}  # Ignore filters, parameters, etc.

    dimensions: list[LookMLField] = Field(default_factory=list, description="Available dimensions")
    measures: list[LookMLField] = Field(default_factory=list, description="Available measures")


class ExploreResponse(BaseModel):
    """Response with full Explore definition."""

    model_config = {"extra": "ignore"}  # Ignore extra fields from API

    name: str = Field(..., description="Explore name")
    label: str | None = Field(None, description="Human-readable label")
    description: str | None = Field(None, description="Explore description")
    model_name: str = Field(..., description="Parent model name")
    view_name: str | None = Field(None, description="Base view name")
    fields: ExploreFields = Field(default_factory=ExploreFields, description="Available fields")
    joins: list[LookMLJoin] = Field(default_factory=list, description="Join relationships")


class ListViewsRequest(APIConfigurableBase):
    """Request to list all views (tables) within an Explore.

    Views represent the underlying tables that make up an Explore.
    Each Explore has a base view and optionally joined views.

    Useful for:
    - Understanding the data model structure
    - Discovering which tables are available in an Explore
    - Seeing join relationships between tables
    """

    model: str = Field(..., description="Model name", examples=["ecommerce"])
    explore: str = Field(..., description="Explore name", examples=["order_items"])

    @staticmethod
    def get_api_config() -> dict:
        """Get API configuration for this endpoint."""
        return {
            "url_template": "/lookml_models/{model}/explores/{explore}",
            "method": "GET",
            "endpoint": "views",
        }

    def to_template_values(self) -> dict[str, str]:
        """Convert to template values."""
        return {"model": self.model, "explore": self.explore}


class View(BaseModel):
    """A view (table) within an Explore.

    Views are the building blocks of Explores. Each Explore has:
    - A base view (the primary table)
    - Zero or more joined views (related tables)
    """

    name: str = Field(..., description="View identifier (e.g., 'order_items', 'users')")
    label: str | None = Field(None, description="Human-readable label")
    type: ViewType | str = Field(
        ...,
        description="View type: 'base' for primary view, 'joined' for joined views",
    )
    join_type: JoinType | str | None = Field(
        None,
        description="Join type (only for joined views): left_outer, inner, full_outer, cross",
    )
    join_on: str | None = Field(None, description="Join condition SQL (only for joined views)")
    dimension_count: int = Field(..., description="Number of dimensions from this view")
    measure_count: int = Field(..., description="Number of measures from this view")
    field_count: int = Field(..., description="Total number of fields from this view")


class ListViewsResponse(BaseModel):
    """Response with list of views in an Explore."""

    views: list[View] = Field(..., description="Views (tables) in the Explore")
    total_count: int = Field(..., description="Total number of views")
    model_name: str = Field(..., description="Parent model name")
    explore_name: str = Field(..., description="Parent explore name")


# =============================================================================
# 2. Queries (Core Execution)
# =============================================================================


class QueryFilter(BaseModel):
    """A filter to apply to a query. Supports Looker filter expressions."""

    field: str = Field(
        ...,
        description="Field to filter on, in format 'view_name.field_name'",
        examples=["order_items.status", "users.created_date", "orders.total_amount"],
    )
    value: str = Field(
        ...,
        description="Filter expression. Supports: exact match ('completed'), "
        "comparison ('>100', '<=50'), range ('100 to 500'), pattern ('%search%'), "
        "negation ('NOT cancelled'), multiple values ('pending,processing'), "
        "relative dates ('last 7 days', 'this month')",
        examples=["completed", ">100", "last 30 days", "NOT cancelled", "pending,processing"],
    )


class TableCalculation(BaseModel):
    """Table calculation definition for dynamic fields.

    Table calculations are computed expressions that operate on query results.
    Supported expressions include percent of total and basic arithmetic operations.
    """

    table_calculation: str = Field(
        ...,
        description="Field name for the calculated value (e.g., 'pct', 'profit')",
        examples=["pct", "profit"],
    )
    expression: str = Field(
        ...,
        description=(
            "Expression to evaluate. Supported: "
            "${field} / sum(${field}) for percent of total, "
            "basic arithmetic (${field1} + - * / ${field2})"
        ),
        examples=["${orders.count} / sum(${orders.count})", "${revenue} - ${cost}"],
    )
    label: str | None = Field(
        None, description="Display label for the calculated field", examples=["Rank", "% of Total"]
    )
    value_format_name: str | None = Field(
        None,
        description="Format name (e.g., 'percent_2', 'decimal_0', 'currency')",
        examples=["percent_2", "decimal_0"],
    )


class CreateQueryRequest(APIConfigurableBase):
    """Request to create a new query."""

    model: str = Field(..., description="Model name", examples=["ecommerce"])
    view: str = Field(..., description="Explore/view name", examples=["order_items"])
    fields: list[str] = Field(
        ...,
        min_length=1,
        description="Fields to include (dimensions and measures)",
        examples=[["order_items.status", "order_items.count"]],
    )
    filters: list[QueryFilter] = Field(
        default_factory=list, description="Filters to apply", examples=[[]]
    )
    sorts: list[str] = Field(
        default_factory=list,
        description="Sort order",
        examples=[["order_items.count desc"]],
    )
    limit: int = Field(
        5000,
        description="Maximum rows to return. Default and max: 5000. "
        "Use smaller limits (100-500) for faster responses when exploring data.",
        ge=1,
        le=5000,
    )
    vis_config: VisConfig | None = Field(
        None,
        description=(
            "Visualization configuration for chart rendering. "
            "Set 'type' to one of: looker_column (vertical bars), looker_bar (horizontal), "
            "looker_line, looker_pie, looker_area, looker_scatter, single_value, table"
        ),
        examples=[{"type": "looker_column"}],
    )
    dynamic_fields: list[TableCalculation] = Field(
        default_factory=list,
        description=(
            "Table calculations to apply to query results. "
            "Supported: ${field} / sum(${field}) for percent of total, "
            "basic arithmetic (${field1} + - * / ${field2})"
        ),
        examples=[
            [
                {
                    "table_calculation": "pct",
                    "label": "% of Total",
                    "expression": "${orders.count} / sum(${orders.count})",
                },
                {
                    "table_calculation": "profit",
                    "label": "Profit",
                    "expression": "${revenue} - ${cost}",
                },
            ]
        ],
    )

    @field_validator("vis_config", mode="before")
    @classmethod
    def parse_vis_config(cls, v):
        """Parse vis_config from JSON string or dict."""
        if v is None or v == "":
            return None
        if isinstance(v, str):
            import json

            try:
                v = json.loads(v)
            except json.JSONDecodeError as e:
                raise ValueError(f"vis_config must be valid JSON: {e}")
        # Convert dict to VisConfig
        if isinstance(v, dict):
            return VisConfig(**v)
        return v

    @staticmethod
    def get_api_config() -> dict:
        """Get API configuration for this endpoint."""
        return {
            "url_template": "/queries",
            "method": "POST",
            "endpoint": "queries",
            "response_key": "query",
        }

    def to_template_values(self) -> dict[str, str]:
        """Convert to template values."""
        return {
            "model": self.model,
            "view": self.view,
        }

    def to_api_body(self) -> dict:
        """Convert to Looker API body format.

        Looker API expects:
        - filters as a dict (field -> value), not a list of QueryFilter objects
        - dynamic_fields as a JSON string, not a list
        - limit as a string, not an integer
        """
        # Convert filters list to dict format expected by Looker API
        filters_dict = {}
        for f in self.filters:
            if f.field in filters_dict:
                # Append to existing value with comma (Looker OR syntax)
                filters_dict[f.field] = f"{filters_dict[f.field]},{f.value}"
            else:
                filters_dict[f.field] = f.value

        body = {
            "model": self.model,
            "view": self.view,
            "fields": self.fields,
            "filters": filters_dict,
            "sorts": self.sorts,
            "limit": str(self.limit),
        }

        # Add vis_config if present
        if self.vis_config:
            body["vis_config"] = self.vis_config.model_dump(exclude_none=True)

        # Add dynamic_fields if present (Looker API expects JSON string)
        if self.dynamic_fields:
            body["dynamic_fields"] = json.dumps(
                [
                    {
                        k: v
                        for k, v in {
                            "table_calculation": df.table_calculation,
                            "expression": df.expression,
                            "label": df.label,
                            "value_format_name": df.value_format_name,
                        }.items()
                        if v is not None
                    }
                    for df in self.dynamic_fields
                ]
            )

        return body

    @classmethod
    def create_repository(cls, response_class: type[BaseModel]):
        """Create a custom repository for query creation.

        In offline mode: Uses custom logic to store queries in shared query store.
        In online mode: Falls back to standard REST API via parent implementation.
        """
        import repository_factory

        if repository_factory.settings.is_offline_mode():
            return repository_factory._create_query_repository(response_class)

        # Fall back to parent implementation for online mode
        return super().create_repository(response_class)


class Query(BaseModel):
    """A saved query definition."""

    id: int | str = Field(
        ..., description="Query ID (integer in offline mode, string slug in online mode)"
    )
    model: str = Field(..., description="Model name")
    view: str = Field(..., description="Explore/view name")
    fields: list[str] = Field(..., min_length=1, description="Selected fields")
    filters: dict[str, list[str]] | None = Field(
        None, description="Applied filters (field -> list of values)"
    )
    sorts: list[str] = Field(default_factory=list, description="Sort order")
    limit: int = Field(5000, description="Row limit")
    dynamic_fields: list[TableCalculation] | None = Field(
        None,
        description="Table calculations - computed fields evaluated on query results",
    )
    vis_config: VisConfig | dict[str, Any] | None = Field(
        None,
        description=(
            "Visualization configuration for chart rendering. "
            "Chart types: looker_column, looker_bar, looker_line, looker_pie, "
            "looker_area, looker_scatter, single_value, table"
        ),
    )

    @field_validator("vis_config", mode="before")
    @classmethod
    def parse_vis_config(cls, v):
        """Parse vis_config from dict to VisConfig if possible."""
        if v is None:
            return None
        if isinstance(v, dict):
            try:
                return VisConfig(**v)
            except Exception:
                # If it doesn't match VisConfig schema, keep as dict for backward compat
                return v
        return v

    @field_validator("filters", mode="before")
    @classmethod
    def convert_null_filters(cls, v):
        """Convert null filters to empty dict."""
        if v is None:
            return {}
        return v


class CreateQueryResponse(BaseModel):
    """Response after creating a query."""

    query: Query = Field(..., description="Created query definition")


class RunQueryRequest(APIConfigurableBase):
    """Request to run a query inline (without saving)."""

    model: str = Field(..., description="Model name", examples=["ecommerce"])
    view: str = Field(..., description="Explore/view name", examples=["order_items"])
    fields: list[str] = Field(
        ...,
        min_length=1,
        description="Fields to include",
        examples=[["order_items.status", "order_items.count"]],
    )
    filters: list[QueryFilter] = Field(default_factory=list, description="Filters to apply")
    sorts: list[str] = Field(default_factory=list, description="Sort order")
    limit: int = Field(
        5000,
        description="Maximum rows to return. Default and max: 5000. "
        "Use smaller limits (100-500) for faster responses when exploring data.",
        ge=1,
        le=5000,
    )
    dynamic_fields: list[TableCalculation] = Field(
        default_factory=list,
        description=(
            "Table calculations to apply to query results. "
            "Supported: ${field} / sum(${field}) for percent of total, "
            "basic arithmetic (${field1} + - * / ${field2})"
        ),
        examples=[
            [
                {
                    "table_calculation": "pct",
                    "label": "% of Total",
                    "expression": "${orders.count} / sum(${orders.count})",
                },
                {
                    "table_calculation": "profit",
                    "label": "Profit",
                    "expression": "${revenue} - ${cost}",
                },
            ]
        ],
    )

    @staticmethod
    def get_api_config() -> dict:
        """Get API configuration for this endpoint."""
        return {"url_template": "/queries/run/json", "method": "POST", "endpoint": "queries"}

    def to_template_values(self) -> dict[str, str]:
        """Convert to template values."""
        return {
            "model": self.model,
            "view": self.view,
        }

    def to_api_body(self) -> dict:
        """Convert to Looker API body format.

        Looker API expects filters as a dict, not a list:
        {"filters": {"field_name": "value"}} not {"filters": [{"field": "x", "value": "y"}]}
        """
        # Convert filters list to dict format expected by Looker API
        filters_dict = {}
        for f in self.filters:
            # Looker API uses field name as key, value as value
            # Multiple values for same field use comma separation or expressions
            if f.field in filters_dict:
                # Append to existing value with comma (Looker OR syntax)
                filters_dict[f.field] = f"{filters_dict[f.field]},{f.value}"
            else:
                filters_dict[f.field] = f.value

        body = {
            "model": self.model,
            "view": self.view,
            "fields": self.fields,
            "filters": filters_dict,
            "sorts": self.sorts,
            "limit": str(self.limit),
        }

        # Add dynamic_fields if present (Looker API expects "dynamic_fields" as JSON string)
        if self.dynamic_fields:
            body["dynamic_fields"] = json.dumps(
                [
                    {
                        k: v
                        for k, v in {
                            "table_calculation": df.table_calculation,
                            "expression": df.expression,
                            "label": df.label,
                            "value_format_name": df.value_format_name,
                        }.items()
                        if v is not None
                    }
                    for df in self.dynamic_fields
                ]
            )

        return body

    @classmethod
    def create_repository(cls, response_class: type[BaseModel]):
        """Create a custom repository for inline query execution.

        In offline mode: Uses DuckDB to execute queries against loaded data.
        In online mode: Uses custom live repository to wrap bare list API response.
        """
        import repository_factory

        if repository_factory.settings.is_offline_mode():
            return repository_factory._create_inline_query_repository(response_class)
        else:
            # Online mode: use custom live repository to wrap bare list response
            return repository_factory._create_run_query_inline_live_repository(response_class)


class RunQueryByIdRequest(APIConfigurableBase):
    """Request to run a saved query by ID."""

    query_id: int | str = Field(..., description="Query ID to execute")

    @staticmethod
    def get_api_config() -> dict:
        """Get API configuration for this endpoint."""
        return {
            "url_template": "/queries/{query_id}/run/json",
            "method": "GET",
            "endpoint": "queries",
        }

    def to_template_values(self) -> dict[str, str]:
        """Convert to template values for URL substitution."""
        return {"query_id": str(self.query_id)}

    @classmethod
    def create_repository(cls, response_class: type[BaseModel]):
        """Create a custom repository for running queries by ID.

        In offline mode: Looks up query from store and executes via DuckDB.
        In online mode: Uses custom live repository that wraps list responses in QueryResult.
        """
        import repository_factory

        if repository_factory.settings.is_offline_mode():
            return repository_factory._create_run_query_repository(response_class)

        # Online mode: use custom live repository that wraps list responses
        return repository_factory._create_run_query_by_id_live_repository(response_class)


class QueryResult(BaseModel):
    """Query execution result."""

    data: list[dict[str, Any]] = Field(..., description="Result rows (JSON format)")
    fields: list[str] = Field(..., description="Field names in result")
    row_count: int = Field(..., description="Number of rows returned")
    sql: str | None = Field(None, description="Generated SQL (if available)")


class RunQueryPngRequest(APIConfigurableBase):
    """Request to run a saved query and return results as a PNG visualization.

    This endpoint executes a query by ID and renders the results as a chart image.
    The visualization type is determined by the query's vis_config if available,
    or defaults to a column chart.

    Use cases:
    - Generate chart images for reports
    - Create visualizations for dashboards
    - Export query results as images for sharing

    Supported chart types:
    - looker_column (default)
    - looker_bar
    - looker_line
    - looker_pie
    - looker_area
    - looker_scatter
    - single_value
    - table
    """

    query_id: int | str = Field(..., description="Query ID to execute and visualize")
    width: int = Field(800, description="Image width in pixels", ge=100, le=4000)
    height: int = Field(600, description="Image height in pixels", ge=100, le=4000)
    chart_type: str | None = Field(
        None,
        description="Override chart type (e.g., 'looker_column', 'looker_bar', 'looker_line'). "
        "If not specified, uses the query's vis_config or defaults to 'looker_column'.",
    )

    @staticmethod
    def get_api_config() -> dict:
        """Get API configuration for this endpoint."""
        return {
            "url_template": "/queries/{query_id}/run/png",
            "method": "GET",
            "endpoint": "queries",
        }

    def to_template_values(self) -> dict[str, str]:
        """Convert to template values for URL substitution."""
        return {"query_id": str(self.query_id)}

    @classmethod
    def create_repository(cls, response_class: type[BaseModel]):
        """Create a custom repository for running queries as PNG.

        In offline mode: Uses chart rendering to generate PNG from DuckDB data.
        In online mode: Uses custom binary response handler (Looker API returns
        raw PNG bytes, not JSON).
        """
        import repository_factory

        if repository_factory.settings.is_offline_mode():
            return repository_factory._create_run_query_png_repository(response_class)

        # Online mode: use custom live repository that handles binary PNG responses
        # (standard LiveDataRepository calls response.json() which fails for binary data)
        return repository_factory._create_run_query_png_live_repository(response_class)


class RunQueryPngResponse(BaseModel):
    """Response containing PNG image data from a query visualization.

    The image is returned as base64-encoded PNG data that can be decoded
    and saved as an image file or displayed in a browser.
    """

    query_id: int | str = Field(..., description="Query ID that was executed")
    image_data: str = Field(..., description="Base64-encoded PNG image data")
    content_type: str = Field("image/png", description="MIME type of the image")
    width: int = Field(..., description="Image width in pixels")
    height: int = Field(..., description="Image height in pixels")
    chart_type: str = Field(..., description="Type of chart rendered")


class ExportQueryRequest(BaseModel):
    """Request to export query results in JSON or CSV format.

    Perfect for:
    - Downloading query results for external analysis
    - Sharing data with other tools
    - Creating data exports for reporting
    """

    query_id: int | str = Field(..., description="Query ID to export")
    format: ExportFormat = Field(
        ExportFormat.JSON,
        description="Export format: json (structured data) or csv (comma-separated)",
    )
    limit: int | None = Field(None, description="Max rows to export (default 5000)", ge=1, le=5000)


class ExportQueryResponse(BaseModel):
    """Response with exported query data."""

    format: str = Field(..., description="Export format used")
    data: Any = Field(..., description="Exported data (list of dicts for JSON, string for CSV)")
    row_count: int = Field(..., description="Number of rows exported")


class RunSqlRequest(APIConfigurableBase):
    """Request to execute arbitrary SQL via Looker SQL Runner.

    SQL Runner allows direct SQL execution against the database,
    bypassing the LookML semantic layer for ad-hoc analysis.

    Perfect for:
    - Advanced SQL analysis
    - Testing and debugging queries
    - Exploring database schema directly
    - Quick data exploration without creating LookML
    """

    connection: str = Field(..., description="Database connection name", examples=["production_db"])
    sql: str = Field(
        ..., description="SQL query to execute", examples=["SELECT * FROM users LIMIT 10"]
    )
    limit: int = Field(
        5000, description="Maximum rows to return (default 5000, max 5000)", ge=1, le=5000
    )

    @staticmethod
    def get_api_config() -> dict:
        """Get API configuration for this endpoint.

        Note: This is not used directly. SQL Runner uses a 2-step workflow:
        1. POST /sql_queries (create query) -> returns slug
        2. POST /sql_queries/{slug}/run/json (execute query) -> returns results

        This config is here for consistency with other request types.
        """
        return {
            "url_template": "/sql_queries",
            "method": "POST",
        }

    def to_template_values(self) -> dict[str, str]:
        """Convert to template values."""
        return {}

    @staticmethod
    def create_repository(response_class: type[BaseModel]) -> Any:
        """Create repository for SQL Runner with 2-step workflow.

        Overrides the default repository creation to handle the Looker API's
        2-step SQL execution workflow (create → run).

        Args:
            response_class: The response model class (SqlQueryResult)

        Returns:
            Repository instance configured for current mode
        """
        from repository_factory import _create_sql_runner_repository

        return _create_sql_runner_repository(response_class)


class SqlQueryResult(BaseModel):
    """Result from SQL Runner query execution."""

    data: list[dict[str, Any]] = Field(..., description="Result rows (JSON format)")
    fields: list[str] = Field(..., description="Column names in result")
    row_count: int = Field(..., description="Number of rows returned")
    runtime_seconds: float | None = Field(None, description="Query execution time")
    connection: str = Field(..., description="Database connection used")
    sql: str = Field(..., description="Executed SQL query")


# =============================================================================
# 3. Content Discovery (Nice-to-Have)
# =============================================================================


class ListFoldersRequest(APIConfigurableBase):
    """List all folders containing Looks and Dashboards.

    Folders help discover content organization and existing query patterns.
    Nice-to-have for:
    - Understanding content structure
    - Finding related reports
    - Browsing by category

    No parameters needed - lists all accessible folders.
    """

    pass

    @staticmethod
    def get_api_config() -> dict:
        """Get API configuration for this endpoint."""
        return {
            "url_template": "/folders",
            "method": "GET",
            "endpoint": "folders",
            "response_key": "folders",
        }

    def to_template_values(self) -> dict[str, str]:
        """Convert to template values."""
        return {}


class Folder(BaseModel):
    """A folder containing Looks and Dashboards."""

    id: str = Field(..., description="Folder ID")
    name: str = Field(..., description="Folder name")
    parent_id: str | None = Field(None, description="Parent folder ID")
    content_metadata_id: int | None = Field(None, description="Content metadata ID")
    child_count: int = Field(0, description="Number of child items")


class ListFoldersResponse(BaseModel):
    """Response with all folders."""

    folders: list[Folder] = Field(..., description="Available folders")


class ListLooksRequest(APIConfigurableBase):
    """List saved Looks (query visualizations).

    Looks represent saved queries with visualizations. They encode
    useful query patterns that agents can learn from.

    Nice-to-have for:
    - Discovering existing query patterns
    - Finding queries by topic
    - Getting query IDs for execution
    """

    folder_id: str | None = Field(None, description="Filter by folder ID")
    title: str | None = Field(None, description="Search by title (case-insensitive)")
    limit: int = Field(100, description="Maximum number of looks to return")

    @staticmethod
    def get_api_config() -> dict:
        """Get API configuration for this endpoint.

        Uses /looks/search endpoint which supports filtering by folder_id, title, etc.
        The basic /looks endpoint does NOT support folder_id filtering.
        See: https://docs.cloud.google.com/looker/docs/reference/looker-api/latest/methods/Look/search_looks
        """
        return {
            "url_template": "/looks/search",
            "method": "GET",
            "endpoint": "looks",
            "response_key": "looks",
        }

    def to_template_values(self) -> dict[str, str]:
        """Convert to template values for search_looks endpoint."""
        values = {}
        if self.folder_id:
            values["folder_id"] = self.folder_id
        if self.title:
            values["title"] = self.title
        if self.limit:
            values["limit"] = str(self.limit)
        return values

    @classmethod
    def create_repository(cls, response_class: type[BaseModel]):
        """Create custom repository for look listing with filtering and pagination.

        In offline mode: Implements custom filtering by folder_id, title, and limit.
        In online mode: Falls back to standard REST API via parent implementation.
        """
        import repository_factory

        if repository_factory.settings.is_offline_mode():
            return repository_factory._create_looks_repository(response_class)

        # Fall back to parent implementation for online mode
        return super().create_repository(response_class)


class Look(BaseModel):
    """A saved Look (query visualization)."""

    id: int | str = Field(..., description="Look ID (can be int or string)")
    title: str = Field(..., description="Look title")
    description: str | None = Field(None, description="Look description")
    folder_id: str | None = Field(None, description="Parent folder ID")
    query_id: str | int | None = Field(
        None, description="Associated query ID (can be string or int)"
    )
    model: str | dict | None = Field(None, description="Model name or model object")
    explore: str | None = Field(None, description="Explore name")
    vis_config: VisConfig | dict | None = Field(
        None, description="Visualization configuration for chart type"
    )


class ListLooksResponse(BaseModel):
    """Response with all Looks."""

    looks: list[Look] = Field(..., description="Available Looks")


class GetLookRequest(APIConfigurableBase):
    """Get a specific Look by ID.

    Retrieves the complete definition of a saved Look, including
    its query configuration and metadata.

    Use cases:
    - Get Look details before running
    - Inspect query configuration
    - Access associated query ID
    """

    look_id: int | str = Field(..., description="Look ID to retrieve")

    @staticmethod
    def get_api_config() -> dict:
        """Get API configuration for this endpoint."""
        return {
            "url_template": "/looks/{look_id}",
            "method": "GET",
            "endpoint": "looks",
        }

    def to_template_values(self) -> dict[str, str]:
        """Convert to template values for URL."""
        return {"look_id": str(self.look_id)}

    def matches(self, lookup_key: dict) -> bool:
        """Match by look_id, or match all if lookup_key is empty (fallback)."""
        if not lookup_key:
            return True

        # Handle both string and int look_id (API may pass as string, mock uses int)
        lookup_look_id = lookup_key.get("look_id")
        if lookup_look_id is None:
            return False

        # Try both direct comparison and string/int conversion
        if lookup_look_id == self.look_id:
            return True

        # Try converting both to string for comparison
        return str(lookup_look_id) == str(self.look_id)

    @staticmethod
    def tool_name() -> str:
        """Return custom tool name to avoid get_get_look."""
        return "get_look"

    @classmethod
    def create_repository(cls, response_class: type[BaseModel]):
        """Create custom repository for getting a Look.

        In offline mode: Checks LOOKS store and shared look store for dynamically created Looks.
        In online mode: Falls back to standard REST API via parent implementation.
        """
        import repository_factory

        if repository_factory.settings.is_offline_mode():
            return repository_factory._create_get_look_repository(response_class)

        # Fall back to parent implementation for online mode
        return super().create_repository(response_class)


class GetLookResponse(Look):
    """Response with a single Look.

    The Looker API returns Look fields directly at the root level,
    so this extends Look rather than wrapping it.
    """

    pass


class RunLookRequest(APIConfigurableBase):
    """Run a saved Look by ID.

    Executes the query associated with a Look and returns the results.
    This is a convenience wrapper that combines get_look + run_query_by_id.

    Use cases:
    - Execute saved Looks without knowing query details
    - Run pre-configured reports
    - Access curated data views
    """

    look_id: int | str = Field(..., description="Look ID to execute")

    @staticmethod
    def get_api_config() -> dict:
        """API configuration for running a Look."""
        return {
            "url_template": "/looks/{look_id}/run/json",
            "method": "GET",
            "endpoint": "looks",
        }

    def to_template_values(self) -> dict[str, str]:
        """Convert request to URL template values."""
        return {"look_id": str(self.look_id)}

    @classmethod
    def create_repository(cls, response_class: type[BaseModel]):
        """Create a repository that executes a Look.

        In offline mode: Uses FunctionalRepository to chain GetLook + RunQueryById.
        In online mode: Uses custom repository to wrap raw API response.
        """
        import repository_factory

        if repository_factory.settings.is_offline_mode():
            return repository_factory._create_run_look_repository(response_class)
        else:
            # Online mode: use custom repository to wrap raw list response
            return repository_factory._create_run_look_live_repository(response_class)


class RunLookResponse(BaseModel):
    """Response with query results from a Look."""

    result: QueryResult = Field(..., description="Query execution results")


class RunLookPdfRequest(APIConfigurableBase):
    """Run a saved Look and return results as a PDF document.

    This endpoint executes the Look's query and renders the visualization
    as a PDF file suitable for printing or sharing.

    Use cases:
    - Generate printable reports from saved Looks
    - Create PDF exports for email distribution
    - Archive Look results as documents
    - Enable LLM analysis via pdfs_read_image tool

    The PDF includes the Look's visualization rendered at the specified
    dimensions with optional page formatting.
    """

    look_id: int | str = Field(..., description="Look ID to render as PDF")
    width: int = Field(800, description="PDF width in pixels", ge=100, le=4000)
    height: int = Field(600, description="PDF height in pixels", ge=100, le=4000)

    @staticmethod
    def get_api_config() -> dict:
        """Get API configuration for this endpoint."""
        return {
            "url_template": "/looks/{look_id}/run/pdf",
            "method": "GET",
            "endpoint": "looks",
        }

    def to_template_values(self) -> dict[str, str]:
        """Convert to template values for URL substitution."""
        return {"look_id": str(self.look_id)}

    @classmethod
    def create_repository(cls, response_class: type[BaseModel]):
        """Create a custom repository for running Looks as PDF.

        In offline mode: Uses PDF rendering to generate document from DuckDB data.
        In online mode: Uses custom binary response handler (Looker API returns
        raw PDF bytes, not JSON).
        """
        import repository_factory

        if repository_factory.settings.is_offline_mode():
            return repository_factory._create_run_look_pdf_repository(response_class)

        # Online mode: use custom live repository that handles binary PDF responses
        return repository_factory._create_run_look_pdf_live_repository(response_class)


class RunLookPdfResponse(BaseModel):
    """Response containing PDF document data from a Look visualization.

    The PDF is returned as base64-encoded data that can be decoded
    and saved as a PDF file or processed by document tools.
    """

    look_id: int | str = Field(..., description="Look ID that was rendered")
    image_data: str = Field(..., description="Base64-encoded PDF data")
    content_type: str = Field("application/pdf", description="MIME type of the document")
    width: int = Field(..., description="PDF width in pixels")
    height: int = Field(..., description="PDF height in pixels")


class SearchContentRequest(APIConfigurableBase):
    """Search for content (Looks, Dashboards) by text query.

    Full-text search across content titles and descriptions.

    Nice-to-have for:
    - Finding content by keywords
    - Discovering related queries
    - Exploring by topic
    """

    query: str = Field(..., description="Search query text", alias="terms")
    types: list[ContentType | str] = Field(
        default_factory=lambda: [ContentType.LOOK, ContentType.DASHBOARD],
        description="Content types to search: look (saved queries) or dashboard (collections)",
    )
    limit: int = Field(100, description="Max results", ge=1, le=1000)

    model_config = {"populate_by_name": True}  # Allow both 'query' and 'terms'

    @field_validator("types", mode="before")
    @classmethod
    def parse_types(cls, v):
        """Parse types from comma-separated string if needed."""
        if isinstance(v, str):
            return [t.strip() for t in v.split(",") if t.strip()]
        return v

    @staticmethod
    def get_api_config() -> dict:
        """Get API configuration for this endpoint.

        Uses GET /content/{terms} endpoint for content search.
        Search terms go in the URL path (will be URL-encoded by LiveDataRepository).
        See: https://docs.cloud.google.com/looker/docs/reference/looker-api/latest/methods/Content/search_content
        """
        return {"url_template": "/content/{terms}", "method": "GET", "endpoint": "search"}

    def to_template_values(self) -> dict[str, str]:
        """Convert to template values for search_content endpoint.

        Search terms go in the URL path as {terms}.
        Other parameters (types, limit) go as query parameters.
        """
        # Ensure types are converted to strings (handle enum values)
        values = {
            "terms": self.query,  # Search terms go in the URL path
            "types": ",".join(self.types),
            "limit": str(self.limit),
        }
        return values

    def matches(self, lookup_key: dict[str, Any]) -> bool:
        """Check if this request matches the lookup key.

        Custom matching logic to handle types field normalization.
        """
        if not lookup_key:
            return True

        # Check query
        if "query" in lookup_key and lookup_key["query"] != self.query:
            return False

        # Check types - normalize both sides to sorted comma-separated string
        if "types" in lookup_key:
            lookup_types = lookup_key["types"]
            if isinstance(lookup_types, list):
                lookup_types = ",".join(sorted(lookup_types))
            request_types = ",".join(sorted(self.types))
            if lookup_types != request_types:
                return False

        # Check limit - normalize both to string
        if "limit" in lookup_key and str(lookup_key["limit"]) != str(self.limit):
            return False

        return True

    @classmethod
    def create_repository(cls, response_class: type[BaseModel]):
        """Create a custom repository for content search.

        In offline mode: Searches both mock and dynamic Looks/Dashboards.
        In online mode: Falls back to standard REST API via parent implementation.
        """
        import repository_factory

        if repository_factory.settings.is_offline_mode():
            return repository_factory._create_search_content_repository(response_class)

        # Fall back to parent implementation for online mode
        return super().create_repository(response_class)


class ContentSearchResult(BaseModel):
    """A single content search result."""

    id: str = Field(..., description="Content ID")
    title: str = Field(..., description="Content title")
    description: str | None = Field(None, description="Content description")
    type: str = Field(..., description="Content type", examples=["look", "dashboard"])
    url: str | None = Field(None, description="Content URL")


class SearchContentResponse(BaseModel):
    """Response with search results."""

    results: list[ContentSearchResult] = Field(..., description="Search results")
    total: int = Field(..., description="Total matching items")


# =============================================================================
# 4. Dashboards (Nice-to-Have)
# =============================================================================


class ListDashboardsRequest(APIConfigurableBase):
    """List available dashboards with optional filtering.

    Dashboards are collections of visualizations and metrics that provide
    comprehensive views of business data. They contain multiple tiles showing
    different aspects of the data.

    Nice-to-have for:
    - Discovering existing dashboard patterns
    - Finding dashboards by topic
    - Understanding available metrics
    """

    search: str | None = Field(None, description="Search dashboards by title (case-insensitive)")
    folder_id: str | None = Field(None, description="Filter by folder ID")
    sorts: list[str] | None = Field(
        None, description="Sort fields (e.g., ['title', '-created_at'])"
    )
    limit: int = Field(100, description="Maximum number of dashboards to return")

    @staticmethod
    def get_api_config() -> dict:
        """Get API configuration for this endpoint.

        Uses /dashboards/search endpoint which supports filtering, sorting, and pagination.
        See: https://docs.cloud.google.com/looker/docs/reference/looker-api/latest/methods/Dashboard/search_dashboards
        """
        return {
            "url_template": "/dashboards/search",
            "method": "GET",
            "endpoint": "dashboards",
            "response_key": "dashboards",
        }

    def to_template_values(self) -> dict[str, str]:
        """Convert to template values for search_dashboards endpoint."""
        values = {}
        if self.folder_id:
            values["folder_id"] = self.folder_id
        if self.search:
            values["title"] = self.search  # search_dashboards uses 'title' parameter
        if self.sorts:
            values["sorts"] = ",".join(self.sorts)
        if self.limit:
            values["limit"] = str(self.limit)
        return values

    @classmethod
    def create_repository(cls, response_class: type[BaseModel]):
        """Create custom repository for dashboard listing with filtering/sorting.

        In offline mode: Implements custom filtering, searching, sorting, and pagination.
        In online mode: Falls back to standard REST API via parent implementation.
        """
        import repository_factory

        if repository_factory.settings.is_offline_mode():
            return repository_factory._create_dashboard_repository(response_class)

        # Fall back to parent implementation for online mode
        return super().create_repository(response_class)


class DashboardTileInfo(BaseModel):
    """Information about a dashboard tile (simplified for listing)."""

    id: str = Field(..., description="Tile ID")
    title: str | None = Field(None, description="Tile title")
    type: TileType | str = Field(
        ...,
        description="Tile visualization type: looker_column, looker_bar, looker_line, "
        "looker_pie, looker_area, looker_scatter, single_value, table, text, button",
    )


class DashboardInfo(BaseModel):
    """Dashboard information (without full tile query definitions)."""

    id: str | int = Field(..., description="Dashboard ID")
    title: str | None = Field(None, description="Dashboard title")
    description: str | None = Field(None, description="Dashboard description")
    folder_id: str | None = Field(None, description="Parent folder ID")
    tile_count: int | None = Field(None, description="Number of tiles in dashboard")
    tiles: list[DashboardTileInfo] | None = Field(None, description="Tile information")
    created_at: str | None = Field(None, description="Creation timestamp")
    updated_at: str | None = Field(None, description="Last update timestamp")


class ListDashboardsResponse(BaseModel):
    """Response with dashboard list."""

    dashboards: list[DashboardInfo] = Field(..., description="Available dashboards")
    total_count: int = Field(..., description="Total number of matching dashboards")


class GetDashboardRequest(APIConfigurableBase):
    """Get a specific dashboard by ID with full tile definitions.

    Returns the complete dashboard including all tile query definitions,
    which is useful for understanding what queries power each visualization.
    """

    dashboard_id: int | str = Field(..., description="Dashboard ID to retrieve")

    @staticmethod
    def get_api_config() -> dict:
        """Get API configuration for this endpoint."""
        return {
            "url_template": "/dashboards/{dashboard_id}",
            "method": "GET",
            "endpoint": "dashboards",
        }

    def to_template_values(self) -> dict[str, str]:
        """Convert to template values."""
        return {"dashboard_id": str(self.dashboard_id)}

    @classmethod
    def create_repository(cls, response_class: type[BaseModel]):
        """Create custom repository for getting a specific dashboard.

        In offline mode: Looks up dashboard from DASHBOARDS store.
        In online mode: Falls back to standard REST API via parent implementation.
        """
        import repository_factory

        if repository_factory.settings.is_offline_mode():
            return repository_factory._create_get_dashboard_repository(response_class)

        # Fall back to parent implementation for online mode
        return super().create_repository(response_class)


class TileDefinition(BaseModel):
    """Full tile definition including query configuration."""

    id: str = Field(..., description="Tile ID")
    title: str | None = Field(None, description="Tile title (can be null for some tile types)")
    type: TileType | str | None = Field(
        None,
        description="Tile visualization type: looker_column, looker_bar, looker_line, "
        "looker_pie, looker_area, looker_scatter, single_value, table, text, button",
    )
    query: dict[str, Any] | None = Field(None, description="Full query definition for this tile")
    query_id: int | str | None = Field(
        None,
        description="Query ID (if using reference instead of full definition) - "
        "can be int or string slug",
    )
    look_id: int | str | None = Field(
        None,
        description="Look ID (if tile was added from a Look)",
    )


class GetDashboardResponse(BaseModel):
    """Response with full dashboard definition."""

    id: int | str = Field(..., description="Dashboard ID (can be int or string)")
    title: str = Field(..., description="Dashboard title")
    description: str | None = Field(None, description="Dashboard description")
    tiles: list[TileDefinition] = Field(
        default_factory=list,
        description="Tile definitions with queries",
        alias="dashboard_elements",
    )
    filters: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Dashboard-level filters",
        alias="dashboard_filters",
    )
    folder_id: str | None = Field(None, description="Parent folder ID")
    created_at: str | None = Field(None, description="Creation timestamp")
    updated_at: str | None = Field(None, description="Last update timestamp")

    model_config = {"populate_by_name": True}  # Allow both field name and alias


class RunDashboardRequest(APIConfigurableBase):
    """Execute all tile queries for a dashboard.

    Runs all tiles in a dashboard and returns results for each tile.
    Supports dashboard-level filter overrides that apply to all tiles.
    """

    dashboard_id: int | str = Field(..., description="Dashboard ID to execute")
    filters: dict[str, list[str]] = Field(
        default_factory=dict, description="Dashboard-level filter overrides"
    )
    include_png: bool = Field(
        default=False,
        description="Include pre-rendered PNG charts for each tile (for GUI use)",
    )

    @staticmethod
    def get_api_config() -> dict:
        """Get API configuration for this endpoint."""
        return {
            "url_template": "/dashboards/{dashboard_id}/run",
            "method": "POST",
            "endpoint": "dashboards",
        }

    def to_template_values(self) -> dict[str, str]:
        """Convert to template values."""
        return {"dashboard_id": str(self.dashboard_id)}

    def to_api_body(self) -> dict:
        """Convert to Looker API body format.

        Dashboard filters are passed as a dict where keys are filter names
        and values are lists of filter values.
        """
        return {"filters": self.filters}

    @classmethod
    def create_repository(cls, response_class: type[BaseModel]):
        """Create custom repository for running dashboard tiles.

        In offline mode: Executes all tile queries with filter merging via DuckDB.
        In online mode: Gets dashboard and runs each tile's query individually.
        """
        import repository_factory

        if repository_factory.settings.is_offline_mode():
            return repository_factory._create_run_dashboard_repository(response_class)

        # Use custom online repository that fetches dashboard and runs each query
        return repository_factory._create_run_dashboard_online_repository(response_class)


class TileResult(BaseModel):
    """Result from executing a single dashboard tile."""

    tile_id: str = Field(..., description="Tile ID")
    tile_title: str | None = Field(None, description="Tile title")
    tile_type: str | None = Field(None, description="Tile visualization type")
    query_result: QueryResult | None = Field(None, description="Query results (null if error)")
    png: str | None = Field(None, description="Base64-encoded PNG chart image")
    error: str | None = Field(None, description="Error message if tile execution failed")


class RunDashboardResponse(BaseModel):
    """Response with results from all dashboard tiles."""

    dashboard_id: int | str = Field(..., description="Dashboard ID")
    dashboard_title: str = Field(..., description="Dashboard title")
    tiles: list[TileResult] = Field(..., description="Results from each tile")


class RunDashboardPdfRequest(APIConfigurableBase):
    """Run a Dashboard and return results as a PDF document.

    This endpoint executes all dashboard tiles and renders the complete
    dashboard layout as a PDF file suitable for printing or sharing.

    Use cases:
    - Generate printable dashboard reports
    - Create PDF exports for executive summaries
    - Archive dashboard snapshots as documents
    - Enable LLM analysis via pdfs_read_image tool

    The PDF includes all dashboard tiles rendered in their layout positions
    with the specified page dimensions.
    """

    dashboard_id: int | str = Field(..., description="Dashboard ID to render as PDF")
    width: int = Field(1200, description="PDF width in pixels", ge=100, le=4000)
    height: int = Field(800, description="PDF height in pixels", ge=100, le=4000)

    @staticmethod
    def get_api_config() -> dict:
        """Get API configuration for this endpoint."""
        return {
            "url_template": "/dashboards/{dashboard_id}/run/pdf",
            "method": "GET",
            "endpoint": "dashboards",
        }

    def to_template_values(self) -> dict[str, str]:
        """Convert to template values for URL substitution."""
        return {"dashboard_id": str(self.dashboard_id)}

    @classmethod
    def create_repository(cls, response_class: type[BaseModel]):
        """Create a custom repository for running Dashboards as PDF.

        In offline mode: Uses PDF rendering to generate document from DuckDB data.
        In online mode: Uses custom binary response handler (Looker API returns
        raw PDF bytes, not JSON).
        """
        import repository_factory

        if repository_factory.settings.is_offline_mode():
            return repository_factory._create_run_dashboard_pdf_repository(response_class)

        # Online mode: use custom live repository that handles binary PDF responses
        return repository_factory._create_run_dashboard_pdf_live_repository(response_class)


class RunDashboardPdfResponse(BaseModel):
    """Response containing PDF document data from a Dashboard visualization.

    The PDF is returned as base64-encoded data that can be decoded
    and saved as a PDF file or processed by document tools.
    """

    dashboard_id: int | str = Field(..., description="Dashboard ID that was rendered")
    image_data: str = Field(..., description="Base64-encoded PDF data")
    content_type: str = Field("application/pdf", description="MIME type of the document")
    width: int = Field(..., description="PDF width in pixels")
    height: int = Field(..., description="PDF height in pixels")


# =============================================================================
# 5. Utility Tools
# =============================================================================


class HealthCheckRequest(APIConfigurableBase):
    """Verify server status and report loaded resources.

    This tool provides a health check endpoint to verify the server
    is running correctly and reports:
    - Server status (always "ok" in offline mode)
    - Current mode (offline/online)
    - Count of loaded resources (schemas, explores, queries, etc.)

    Use cases:
    - Verify server connectivity
    - Check server configuration
    - Debug resource loading issues
    """

    pass  # No input parameters needed

    @staticmethod
    def get_api_config() -> dict:
        """Get API configuration for this endpoint."""
        return {"url_template": "/health", "method": "GET", "endpoint": "health"}

    def to_template_values(self) -> dict[str, str]:
        """Convert to template values."""
        return {}

    @staticmethod
    def tool_name() -> str:
        """Override default tool name."""
        return "health_check"


class HealthCheckResponse(BaseModel):
    """Response with server health status."""

    status: str = Field(..., description="Server status", examples=["ok"])
    mode: str = Field(..., description="Server mode", examples=["offline", "online", "hybrid"])
    mode_details: str = Field(
        ...,
        description="Detailed mode information (API URL, data file, etc.)",
        examples=[
            "OFFLINE - Using mock data",
            "HYBRID - Using captured data from ../../data/looker/captured/sample.json",
        ],
    )
    schemas_loaded: int = Field(..., description="Number of LookML models loaded")
    explores_loaded: int = Field(..., description="Number of explores loaded")
    saved_queries: int = Field(..., description="Number of saved queries available")
    dashboards: int = Field(0, description="Number of dashboards available")
    looks: int = Field(0, description="Number of looks available")


# =============================================================================
# Exports
# =============================================================================

# Deprecated model names - do not use these in new code
# Map old name -> new name for automated consistency checks
# When renaming a model, add the mapping here and the test suite will
# catch any remaining uses of the old name in the codebase.
DEPRECATED_MODEL_NAMES: dict[str, str] = {
    "DynamicField": "TableCalculation",
}

__all__ = [
    # Enums
    "ChartType",
    "JoinType",
    "RelationshipType",
    "ViewType",
    "ContentType",
    "ExportFormat",
    "FieldType",
    "TileType",
    # Base classes
    "VisConfig",
    "APIConfigurableBase",
    # LookML Models & Explores
    "LookMLModelRequest",
    "LookmlModelNavExplore",
    "LookMLModel",
    "LookMLModelResponse",
    "ExploreRequest",
    "LookMLField",
    "LookMLJoin",
    "ExploreFields",
    "ExploreResponse",
    "ListViewsRequest",
    "View",
    "ListViewsResponse",
    # Queries
    "TableCalculation",
    "QueryFilter",
    "CreateQueryRequest",
    "Query",
    "CreateQueryResponse",
    "RunQueryRequest",
    "RunQueryByIdRequest",
    "QueryResult",
    "RunQueryPngRequest",
    "RunQueryPngResponse",
    "ExportQueryRequest",
    "ExportQueryResponse",
    "RunSqlRequest",
    "SqlQueryResult",
    # Content Discovery
    "ListFoldersRequest",
    "Folder",
    "ListFoldersResponse",
    "ListLooksRequest",
    "Look",
    "ListLooksResponse",
    "GetLookRequest",
    "GetLookResponse",
    "RunLookRequest",
    "RunLookResponse",
    "RunLookPdfRequest",
    "RunLookPdfResponse",
    "SearchContentRequest",
    "ContentSearchResult",
    "SearchContentResponse",
    # Dashboards
    "ListDashboardsRequest",
    "DashboardTileInfo",
    "DashboardInfo",
    "ListDashboardsResponse",
    "GetDashboardRequest",
    "TileDefinition",
    "GetDashboardResponse",
    "RunDashboardRequest",
    "TileResult",
    "RunDashboardResponse",
    "RunDashboardPdfRequest",
    "RunDashboardPdfResponse",
    # Utility
    "HealthCheckRequest",
    "HealthCheckResponse",
    # Metadata for consistency checks
    "DEPRECATED_MODEL_NAMES",
]
