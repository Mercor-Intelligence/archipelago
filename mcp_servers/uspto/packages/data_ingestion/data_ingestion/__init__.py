"""Generic data ingestion framework - source and format agnostic.

This framework provides a reusable architecture for ingesting data from
any source (files, APIs, streams) in any format (XML, JSON, CSV) into
any persistence layer (SQLite, PostgreSQL, MongoDB, etc.).

The framework is completely generic and has zero knowledge of specific
domains (patents, finance, HR, etc.). Applications provide their own:
- Domain models (Pydantic, dataclasses, etc.)
- Persistence implementation (database-specific)
- Business validation rules

Key Components:
    - DataSource: Abstract interface for data sources
    - DataExtractor: Abstract interface for format parsing
    - Persister: Abstract interface for data persistence
    - IngestionPipeline: Orchestrates the ingestion flow
    - IngestionFramework: Main entry point

Example Usage:
    >>> from data_ingestion import IngestionFramework
    >>> from myapp.persistence import MyAppPersister
    >>> from myapp.models import create_my_record
    >>>
    >>> persister = MyAppPersister(db_path='./data/myapp.db')
    >>> framework = IngestionFramework(
    ...     config_file='config/ingestion.yaml',
    ...     persister=persister,
    ...     record_factory=create_my_record
    ... )
    >>> stats = framework.ingest()
    >>> print(f"Processed: {stats.records_processed}")
"""

from .exceptions import (
    ConfigurationError,
    ExtractionError,
    IngestionError,
    PersistenceError,
    SourceError,
    ValidationError,
)
from .extractors import ExtractionResult, XMLExtractor
from .framework import IngestionFramework
from .interfaces import BatchResult, DataExtractor, DataSource, Persister
from .pipeline import IngestionPipeline
from .sources import FileSource
from .stats import IngestionStats

__version__ = "0.1.0"

__all__ = [
    # Interfaces
    "DataSource",
    "DataExtractor",
    "Persister",
    "BatchResult",
    # Sources
    "FileSource",
    # Extractors
    "XMLExtractor",
    "ExtractionResult",
    # Pipeline & Framework
    "IngestionPipeline",
    "IngestionFramework",
    # Exceptions
    "IngestionError",
    "SourceError",
    "ExtractionError",
    "ValidationError",
    "PersistenceError",
    "ConfigurationError",
    # Stats
    "IngestionStats",
]
