"""Abstract interfaces for generic data ingestion framework.

This module defines the core contracts that must be implemented by:
- Data sources (files, APIs, streams, databases)
- Data extractors (XML, JSON, CSV, etc.)
- Data persisters (application-specific storage)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, BinaryIO

if TYPE_CHECKING:
    from .extractors import ExtractionResult


@dataclass
class BatchResult:
    """Result of persisting a batch of records.

    Attributes:
        inserted: Number of records successfully inserted
        errors: Number of records that failed to insert
    """

    inserted: int
    errors: int


class DataSource(ABC):
    """Abstract interface for data sources.

    A DataSource represents where data comes from (file, API, stream, database).
    It provides streaming access to raw data via file handles.
    """

    @abstractmethod
    def stream(self, start_position: Any = None) -> Iterator[BinaryIO]:
        """Stream data via file handles.

        Args:
            start_position: Optional position to start from (for resume support)

        Yields:
            File handles (BinaryIO) to read data from

        Raises:
            SourceError: If source cannot be accessed or read
        """
        pass

    @abstractmethod
    def get_metadata(self) -> dict[str, Any]:
        """Get source metadata.

        Returns:
            Dictionary with source information (type, path, size, etc.)
        """
        pass

    @abstractmethod
    def supports_resume(self) -> bool:
        """Check if source supports resume from a position.

        Returns:
            True if resume is supported, False otherwise
        """
        pass

    @abstractmethod
    def close(self) -> None:
        """Cleanup and release resources.

        Should be called when done with the source.
        """
        pass


class DataExtractor(ABC):
    """Abstract interface for data extractors.

    A DataExtractor parses specific data formats (XML, JSON, CSV)
    and extracts structured records from file handles.
    """

    @abstractmethod
    def extract(self, file_handle: BinaryIO) -> Iterator[ExtractionResult]:
        """Extract records from file handle.

        Args:
            file_handle: File handle (BinaryIO) to read data from

        Yields:
            ExtractionResult objects containing either:
            - Successful record (result.record contains data, result.error is None)
            - Failed record (result.record is None, result.error contains exception)

        Raises:
            ExtractionError: Only for file-level errors (invalid syntax, file not readable)
        """
        pass

    @abstractmethod
    def supports_streaming(self) -> bool:
        """Check if extractor supports streaming.

        Returns:
            True if extractor can stream records efficiently
        """
        pass


class Persister[T](ABC):
    """Abstract interface for data persistence.

    A Persister is implemented by applications to save data to their
    chosen storage backend (SQLite, PostgreSQL, MongoDB, etc.).

    Type parameter T represents the domain model type being persisted.
    """

    @abstractmethod
    def persist_batch(self, records: list[T]) -> BatchResult:
        """Persist a batch of records.

        Args:
            records: List of domain objects to persist

        Returns:
            BatchResult with inserted and error counts

        Raises:
            PersistenceError: Only for critical errors
        """
        pass

    @abstractmethod
    def close(self) -> None:
        """Cleanup and release resources.

        Should be called when done persisting data.
        """
        pass
