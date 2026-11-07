"""Ingestion pipeline orchestration.

This module provides the core pipeline that orchestrates the flow of data
from source through extraction to persistence.
"""

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from .exceptions import ExtractionError, ValidationError
from .interfaces import DataExtractor, DataSource, Persister
from .stats import IngestionStats

logger = logging.getLogger(__name__)


class IngestionPipeline[T]:
    """Orchestrates data ingestion flow.

    Connects source -> extractor -> persister with error handling,
    batching, and statistics tracking.

    Args:
        source: Data source to read from
        extractor: Data extractor to parse records
        persister: Persister to save records
        record_factory: Callable to create domain objects from extracted dicts
        batch_size: Number of records per batch (default: 100)

    Example:
        >>> source = FileSource('/data/file.xml')
        >>> extractor = XMLExtractor(record_tag='record', field_mappings={...})
        >>> persister = MyPersister(db_path='./data.db')
        >>> pipeline = IngestionPipeline(
        ...     source=source,
        ...     extractor=extractor,
        ...     persister=persister,
        ...     record_factory=MyRecord.from_dict
        ... )
        >>> stats = pipeline.run()
    """

    def __init__(
        self,
        source: DataSource,
        extractor: DataExtractor,
        persister: Persister[T],
        record_factory: Callable[[dict[str, Any]], T],
        batch_size: int = 100,
        on_complete: Callable[[IngestionStats], None] | None = None,
    ):
        """Initialize IngestionPipeline.

        Args:
            source: Data source to read from
            extractor: Data extractor to parse records
            persister: Persister to save records
            record_factory: Callable to create domain objects from extracted dicts
            batch_size: Number of records per batch (default: 100)
            on_complete: Optional callback called after all batches processed
        """
        self.source = source
        self.extractor = extractor
        self.persister = persister
        self.record_factory = record_factory
        self.batch_size = batch_size
        self.on_complete = on_complete
        self.stats = IngestionStats(start_time=datetime.now())

    def run(self, start_position: Any = None) -> IngestionStats:
        """Run the ingestion pipeline.

        Args:
            start_position: Optional position to resume from

        Returns:
            IngestionStats with ingestion metrics

        Raises:
            SourceError: If source cannot be accessed (critical error)
        """
        logger.info("Starting ingestion pipeline")

        try:
            # Get metadata inside try block to ensure cleanup on failure
            logger.info(f"Source: {self.source.get_metadata()}")
            # Stream file handles from source
            for file_handle in self.source.stream(start_position=start_position):
                try:
                    # Extract records from file handle
                    for result in self.extractor.extract(file_handle):
                        if result.is_success:
                            # Successfully extracted record - process it
                            assert result.record is not None  # Type narrowing for pyright
                            self._process_record(result.record)
                        else:
                            # Extraction/validation error on this record - log and skip
                            self.stats.records_processed += 1  # Count as processed even if failed
                            if isinstance(result.error, ValidationError):
                                logger.warning(
                                    f"Record validation failed during extraction: {result.error}"
                                )
                                self.stats.validation_errors += 1
                            elif isinstance(result.error, ExtractionError):
                                logger.warning(f"Record extraction failed: {result.error}")
                                self.stats.parse_errors += 1
                            else:
                                logger.error(f"Unexpected error during extraction: {result.error}")
                                self.stats.parse_errors += 1
                            self.stats.records_skipped += 1

                except ExtractionError as e:
                    # File-level extraction error (e.g., invalid XML syntax)
                    logger.warning(f"File extraction failed: {e}")
                    self.stats.parse_errors += 1

            # Flush any remaining records
            self._flush_batch()

            # Mark completion (must be before callback so timing data is available)
            self.stats.end_time = datetime.now()

            # Call completion callback before cleanup
            if self.on_complete:
                self.on_complete(self.stats)

        finally:
            # Cleanup resources - each operation is independent to prevent cascading failures
            try:
                self.source.close()
            except Exception as e:
                logger.error(f"Error closing source: {e}")

            try:
                self.persister.close()
            except Exception as e:
                logger.error(f"Error closing persister: {e}")

            # Ensure end_time is set even if exception occurred before callback
            if self.stats.end_time is None:
                self.stats.end_time = datetime.now()

        logger.info(f"Ingestion complete: {self.stats.records_processed} records processed")
        logger.info(f"Success rate: {self.stats.success_rate:.2f}%")
        logger.info(f"Error rate: {self.stats.error_rate:.2f}%")

        return self.stats

    def _process_record(self, raw_record: dict[str, Any]) -> None:
        """Process a single extracted record.

        Args:
            raw_record: Dictionary of extracted field values
        """
        self.stats.records_processed += 1

        try:
            # Create domain object
            domain_object = self.record_factory(raw_record)

            # Add to batch
            if not hasattr(self, "_batch"):
                self._batch = []
            self._batch.append(domain_object)

            # Flush batch if full
            if len(self._batch) >= self.batch_size:
                self._flush_batch()

        except (ValidationError, PydanticValidationError) as e:
            logger.warning(f"Record validation failed: {e}")
            self.stats.validation_errors += 1
            self.stats.records_skipped += 1

        except Exception as e:
            logger.error(f"Unexpected error processing record: {e}")
            self.stats.validation_errors += 1
            self.stats.records_skipped += 1

    def _flush_batch(self) -> None:
        """Persist current batch of records."""
        if not hasattr(self, "_batch") or not self._batch:
            return

        try:
            result = self.persister.persist_batch(self._batch)
            self.stats.records_inserted += result.inserted
            self.stats.persistence_errors += result.errors
            self.stats.records_skipped += result.errors
            self.stats.batches_completed += 1

            if result.errors > 0:
                logger.info(f"Batch complete: {result.inserted} inserted, {result.errors} errors")
            else:
                logger.debug(f"Batch persisted: {result.inserted} records")

        except Exception as e:
            # Any exception during persistence - treat entire batch as failed
            logger.error(f"Batch persistence failed: {e}", exc_info=True)
            self.stats.persistence_errors += len(self._batch)
            self.stats.records_skipped += len(self._batch)

        finally:
            # Clear batch
            self._batch = []
