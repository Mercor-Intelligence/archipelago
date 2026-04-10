"""Main ingestion framework entry point.

This module provides the high-level IngestionFramework that combines
configuration loading with pipeline execution.
"""

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from .exceptions import ConfigurationError
from .extractors import XMLExtractor
from .interfaces import DataExtractor, DataSource, Persister
from .pipeline import IngestionPipeline
from .sources import FileSource
from .stats import IngestionStats

logger = logging.getLogger(__name__)


class IngestionFramework[T]:
    """Main framework for configurable data ingestion.

    Provides a high-level interface for running ingestion jobs based on
    YAML configuration files.

    Args:
        config_file: Path to YAML configuration file
        persister: Application-specific persister implementation
        record_factory: Callable to create domain objects from dicts

    Example:
        >>> persister = MyPersister(db_path='./data.db')
        >>> framework = IngestionFramework(
        ...     config_file='config/ingestion.yaml',
        ...     persister=persister,
        ...     record_factory=MyRecord.from_dict
        ... )
        >>> stats = framework.ingest()
    """

    def __init__(
        self,
        config_file: str | Path,
        persister: Persister[T],
        record_factory: Callable[[dict[str, Any]], T],
        source: DataSource | None = None,
        on_complete: Callable[[Any], None] | None = None,
    ):
        """Initialize IngestionFramework.

        Args:
            config_file: Path to YAML configuration file
            persister: Application-specific persister implementation
            record_factory: Callable to create domain objects from dicts
            source: Optional custom data source (overrides config)
            on_complete: Optional callback called after all batches processed

        Raises:
            ConfigurationError: If config file is invalid or missing
        """
        self.config_file = Path(config_file)
        self.persister = persister
        self.record_factory = record_factory
        self.custom_source = source
        self.on_complete = on_complete
        self.config = self._load_config()

    def _load_config(self) -> dict[str, Any]:
        """Load and validate configuration from YAML file.

        Returns:
            Dictionary with configuration

        Raises:
            ConfigurationError: If config file is invalid or missing
        """
        if not self.config_file.exists():
            raise ConfigurationError(f"Config file not found: {self.config_file}")

        try:
            with open(self.config_file) as f:
                config = yaml.safe_load(f)

            if not config:
                raise ConfigurationError(f"Empty config file: {self.config_file}")

            # Validate required sections
            if "source" not in config:
                raise ConfigurationError("Missing required config section: source")

            if "extractor" not in config:
                raise ConfigurationError("Missing required config section: extractor")

            return config

        except yaml.YAMLError as e:
            raise ConfigurationError(f"Invalid YAML syntax: {e}") from e
        except OSError as e:
            raise ConfigurationError(f"Cannot read config file: {e}") from e

    def _create_source(self) -> DataSource:
        """Create data source from configuration.

        Returns:
            Configured DataSource instance

        Raises:
            ConfigurationError: If source configuration is invalid
        """
        source_config = self.config["source"]
        source_type = source_config.get("type")

        if source_type == "file":
            file_path = source_config.get("path")
            if not file_path:
                raise ConfigurationError("Missing required source.path for file source")

            # Resolve relative paths relative to config file directory
            file_path_obj = Path(file_path)
            if not file_path_obj.is_absolute():
                file_path_obj = (self.config_file.parent / file_path_obj).resolve()

            return FileSource(file_path=file_path_obj)

        else:
            raise ConfigurationError(f"Unknown source type: {source_type}")

    def _create_extractor(self) -> DataExtractor:
        """Create data extractor from configuration.

        Returns:
            Configured DataExtractor instance

        Raises:
            ConfigurationError: If extractor configuration is invalid
        """
        extractor_config = self.config["extractor"]
        extractor_type = extractor_config.get("type")

        if extractor_type == "xml":
            record_tags = extractor_config.get("record_tags")
            if not record_tags:
                raise ConfigurationError("Missing required extractor.record_tags for xml extractor")

            fields = extractor_config.get("fields")
            if not fields:
                raise ConfigurationError("Missing required extractor.fields for xml extractor")

            namespaces = extractor_config.get("namespaces", {})

            return XMLExtractor(
                record_tags=record_tags,
                fields=fields,
                namespaces=namespaces,
            )

        else:
            raise ConfigurationError(f"Unknown extractor type: {extractor_type}")

    def ingest(self, start_position: Any = None) -> IngestionStats:
        """Run ingestion based on configuration.

        Args:
            start_position: Optional position to resume from

        Returns:
            IngestionStats with ingestion metrics

        Raises:
            ConfigurationError: If configuration is invalid
            SourceError: If source cannot be accessed
        """
        logger.info(f"Starting ingestion from config: {self.config_file}")

        # Create components from config (or use custom source if provided)
        source = self.custom_source or self._create_source()
        extractor = self._create_extractor()

        # Get pipeline settings
        pipeline_config = self.config.get("pipeline", {})
        batch_size = pipeline_config.get("batch_size", 100)

        # Create and run pipeline
        pipeline = IngestionPipeline(
            source=source,
            extractor=extractor,
            persister=self.persister,
            record_factory=self.record_factory,
            batch_size=batch_size,
            on_complete=self.on_complete,
        )

        return pipeline.run(start_position=start_position)
