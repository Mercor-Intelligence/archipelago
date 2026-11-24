"""Repository pattern for EDGAR data access.

Provides abstraction over online (SEC API) and offline (local files) data sources.

Usage:
    from repositories import get_repository

    repo = get_repository()
    data = await repo.get_company_facts("0000320193")
"""

from repositories.base import EdgarRepository
from repositories.factory import get_repository

__all__ = ["EdgarRepository", "get_repository"]
