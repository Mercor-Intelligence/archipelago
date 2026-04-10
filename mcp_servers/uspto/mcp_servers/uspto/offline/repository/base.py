"""Base repository for USPTO offline mode."""

import sqlite3
from typing import Any


class BaseRepository:
    """Base repository class with common database operations."""

    def __init__(self, conn: sqlite3.Connection):
        """Initialize repository with database connection.

        Args:
            conn: SQLite database connection
        """
        self.conn = conn
        self.cursor = conn.cursor()

    def execute(self, query: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute a SQL query.

        Args:
            query: SQL query string
            params: Query parameters

        Returns:
            Cursor with query results
        """
        return self.cursor.execute(query, params)

    def execute_many(self, query: str, params_list: list[tuple]) -> sqlite3.Cursor:
        """Execute a SQL query with multiple parameter sets.

        Args:
            query: SQL query string
            params_list: List of parameter tuples

        Returns:
            Cursor after execution
        """
        return self.cursor.executemany(query, params_list)

    def fetch_one(self, query: str, params: tuple = ()) -> dict[str, Any] | None:
        """Execute query and fetch one row as dictionary.

        Args:
            query: SQL query string
            params: Query parameters

        Returns:
            Row as dictionary or None if no results
        """
        self.cursor.row_factory = sqlite3.Row
        cursor = self.cursor.execute(query, params)
        row = cursor.fetchone()
        self.cursor.row_factory = None
        return dict(row) if row else None

    def fetch_all(self, query: str, params: tuple = ()) -> list[dict[str, Any]]:
        """Execute query and fetch all rows as dictionaries.

        Args:
            query: SQL query string
            params: Query parameters

        Returns:
            List of rows as dictionaries
        """
        self.cursor.row_factory = sqlite3.Row
        cursor = self.cursor.execute(query, params)
        rows = cursor.fetchall()
        self.cursor.row_factory = None
        return [dict(row) for row in rows]

    def commit(self) -> None:
        """Commit the current transaction."""
        self.conn.commit()

    def rollback(self) -> None:
        """Roll back the current transaction."""
        self.conn.rollback()

    def last_insert_rowid(self) -> int:
        """Get the rowid of the last inserted row.

        Returns:
            Last inserted row ID
        """
        return self.cursor.lastrowid
