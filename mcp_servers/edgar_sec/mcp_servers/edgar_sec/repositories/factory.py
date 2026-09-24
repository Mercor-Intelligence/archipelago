"""Factory for creating the appropriate repository based on configuration."""

from config import (
    EDGAR_API_BASE_URL,
    EDGAR_OFFLINE_DATA_DIR,
    EDGAR_OFFLINE_MODE,
    EDGAR_USER_AGENT,
)
from loguru import logger
from repositories.base import EdgarRepository
from repositories.offline import OfflineEdgarRepository
from repositories.online import OnlineEdgarRepository

# Singleton repository instance
_repository: EdgarRepository | None = None


def get_repository() -> EdgarRepository:
    """Get the appropriate repository based on configuration.

    Returns OfflineEdgarRepository if EDGAR_OFFLINE_MODE=true,
    otherwise returns OnlineEdgarRepository.

    The repository is cached as a singleton for efficiency.

    Returns:
        EdgarRepository implementation

    Example:
        repo = get_repository()
        facts = await repo.get_company_facts("0000320193")
    """
    global _repository

    if _repository is not None:
        return _repository

    if EDGAR_OFFLINE_MODE:
        logger.info(f"Using OFFLINE mode with data from: {EDGAR_OFFLINE_DATA_DIR}")
        _repository = OfflineEdgarRepository(EDGAR_OFFLINE_DATA_DIR)
    else:
        logger.info(f"Using ONLINE mode with API: {EDGAR_API_BASE_URL}")
        _repository = OnlineEdgarRepository(EDGAR_API_BASE_URL, EDGAR_USER_AGENT)

    return _repository


def reset_repository() -> None:
    """Reset the repository singleton (useful for testing)."""
    global _repository
    _repository = None
