"""Base seeder class and common types."""

import logging
from abc import ABC, abstractmethod
from enum import Enum

from ..fetchers import BaseFetcher
from ..loaders import BaseLoader
from ..storage import RawStorage

logger = logging.getLogger(__name__)


class SymbolResult(Enum):
    """Result of processing a single symbol."""

    SUCCESS = "success"
    SKIPPED = "skipped"
    NO_DATA = "no_data"
    ERROR = "error"


class BaseSeeder(ABC):
    """Base class for data seeders.

    Seeders orchestrate the fetch -> store -> load flow for a specific
    data type (historical, intraday, profiles).
    """

    def __init__(
        self,
        fetcher: BaseFetcher | None,
        storage: RawStorage,
        loader: BaseLoader,
    ):
        """Initialize the seeder.

        Args:
            fetcher: Data fetcher (can be None if only loading from raw)
            storage: Raw data storage
            loader: Database loader
        """
        self.fetcher = fetcher
        self.storage = storage
        self.loader = loader

    def _require_fetcher(self) -> BaseFetcher:
        """Get fetcher, raising if not available."""
        if self.fetcher is None:
            raise RuntimeError("Fetcher required but not configured.")
        return self.fetcher

    @abstractmethod
    def seed(
        self,
        symbols: list[str],
        save_raw: bool = True,
        from_raw: bool = False,
        raw_only: bool = False,
        force: bool = False,
        verbose: bool = True,
        **kwargs,
    ) -> int:
        """Seed data for the given symbols.

        Args:
            symbols: List of stock symbols
            save_raw: Whether to save raw JSON files
            from_raw: Load from existing raw files instead of API
            raw_only: Only save raw files, don't load to DB
            force: Force re-fetch even if data exists
            verbose: Print progress

        Returns:
            Total rows inserted
        """
        pass
