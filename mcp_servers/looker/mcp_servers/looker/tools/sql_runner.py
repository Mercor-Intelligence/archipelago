"""SQL Runner tools.

Tools for executing arbitrary SQL queries directly against the database,
bypassing the LookML semantic layer.
"""

import sys
import time
from pathlib import Path

import sqlparse
from sqlparse.sql import Statement
from sqlparse.tokens import Keyword

# Add server to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from models import RunSqlRequest, SqlQueryResult
from repository_factory import create_repository


def validate_sql_safety(sql: str) -> None:
    """Validate that SQL query doesn't contain dangerous operations."""
    # Parse the SQL
    parsed = sqlparse.parse(sql)

    # Ensure we have exactly one statement (prevent SQL injection via multiple statements)
    if len(parsed) == 0:
        raise ValueError("Empty SQL query provided")

    if len(parsed) > 1:
        raise ValueError(
            "Multiple SQL statements are not allowed. "
            "Only a single SELECT query is supported for safety."
        )

    statement: Statement = parsed[0]

    # Get the statement type (SELECT, INSERT, UPDATE, DELETE, etc.)
    stmt_type = statement.get_type()

    # Only allow SELECT (and None for some complex queries)
    # IMPORTANT: sqlparse correctly identifies dangerous CTEs like
    # "WITH cte AS (SELECT...) UPDATE..." as UPDATE, not None or SELECT
    # So malicious CTEs are properly blocked by this check
    if stmt_type not in ("SELECT", None):
        raise ValueError(
            f"Only SELECT queries (and CTEs starting with WITH) are allowed. "
            f"Got: {stmt_type}. Other operations are blocked for safety."
        )

    # For None type, verify it's a valid CTE (starts with WITH)
    # This handles edge cases where sqlparse returns None for complex queries
    if stmt_type is None:
        # Get first meaningful token
        first_token = statement.token_first(skip_ws=True, skip_cm=True)
        if first_token is None or first_token.ttype not in (Keyword.CTE, Keyword):
            raise ValueError(
                "Only SELECT queries (and CTEs starting with WITH) are allowed. "
                "Other operations are blocked for safety."
            )

        # Check if it's actually WITH keyword (exact match to prevent bypasses)
        token_value = first_token.value.upper()
        if token_value != "WITH":
            raise ValueError(
                "Only SELECT queries (and CTEs starting with WITH) are allowed. "
                "Other operations are blocked for safety."
            )


async def run_sql_query(request: RunSqlRequest) -> SqlQueryResult:
    """Execute a SQL query via Looker SQL Runner. Only SELECT queries are allowed."""
    # Validate SQL safety
    validate_sql_safety(request.sql)

    # Use repository pattern (handles both offline and online modes)
    repo = create_repository(RunSqlRequest, SqlQueryResult)
    return await repo.get(request)


async def _execute_sql_mock(request: RunSqlRequest) -> SqlQueryResult:
    """Execute SQL query in offline mode with mock data."""
    # Simulate query execution time
    start_time = time.time()

    # Generic mock data - same for all queries
    mock_data = [
        {"column1": "value1", "column2": 123, "column3": "2024-01-01"},
        {"column1": "value2", "column2": 456, "column3": "2024-01-02"},
        {"column1": "value3", "column2": 789, "column3": "2024-01-03"},
    ]

    # Apply limit
    data = mock_data[: request.limit]

    runtime = time.time() - start_time

    return SqlQueryResult(
        data=data,
        fields=list(data[0].keys()) if data else [],
        row_count=len(data),
        runtime_seconds=round(runtime, 3),
        connection=request.connection,
        sql=request.sql,
    )
