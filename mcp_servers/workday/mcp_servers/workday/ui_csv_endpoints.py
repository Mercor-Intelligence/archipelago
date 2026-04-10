"""UI CSV Endpoints for Workday MCP Server.

This module registers shared CSV import/validation REST endpoints with the FastAPI app.
The endpoints provide UI-friendly CSV upload, validation, and import capabilities.

Endpoints registered:
- GET /schema: Returns database schema for UI dropdown population
- POST /validate: Validates CSV files in a ZIP archive (returns sample rows for preview)
- POST /import-validated: Imports validated CSV files into the database

Usage:
    This file is imported by the MCP middleware's REST bridge when HTTP transport
    is enabled. The register_endpoints() function is called automatically.
"""

from db.models import Base
from fastapi import FastAPI
from mcp_scripts.csv_endpoints import register_csv_endpoints


async def register_endpoints(app: FastAPI, module_path: str, engine=None) -> None:
    """Register CSV import/validation REST endpoints on the FastAPI app.

    Called by mcp_middleware's REST bridge when setting up HTTP transport.

    Args:
        app: FastAPI application instance to register endpoints on
        module_path: Module path (unused, for compatibility with REST bridge interface)
        engine: SQLAlchemy async engine for database operations (required)

    Raises:
        ValueError: If engine is None (required for CSV import operations)
    """
    if engine is None:
        raise ValueError(
            "CSV endpoints require a database engine. "
            "Ensure the REST bridge passes the async engine when calling register_endpoints()."
        )

    # Register shared CSV endpoints using the SQLAlchemy Base model
    # This provides schema introspection for all database tables
    register_csv_endpoints(app, Base, engine)
