"""Repository package for BambooHR MCP server.

Provides data access layer for all database operations.
"""

from .employee import EmployeeNotFoundError, EmployeeRepository

__all__ = [
    "EmployeeRepository",
    "EmployeeNotFoundError",
]
