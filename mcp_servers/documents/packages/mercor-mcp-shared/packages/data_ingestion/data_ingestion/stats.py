"""Statistics tracking for ingestion runs.

This module provides the IngestionStats dataclass for tracking metrics
during an ingestion run, including timing, throughput, and error counts.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass
class IngestionStats:
    """Statistics for an ingestion run.

    Tracks timing, throughput, success metrics, and error counts.
    Provides calculated properties for duration, throughput, and error rates.
    """

    # Timing
    start_time: datetime
    end_time: datetime | None = None

    # Throughput
    records_processed: int = 0
    records_inserted: int = 0
    records_skipped: int = 0

    # Errors
    parse_errors: int = 0
    validation_errors: int = 0
    persistence_errors: int = 0

    # Batching
    batches_completed: int = 0

    @property
    def duration(self) -> timedelta | None:
        """Calculate total duration of ingestion.

        Returns:
            Timedelta representing elapsed time, or None if not completed
        """
        if self.end_time:
            return self.end_time - self.start_time
        return None

    @property
    def duration_seconds(self) -> float:
        """Calculate duration in seconds.

        Returns:
            Duration in seconds, or 0.0 if not completed
        """
        if self.duration:
            return self.duration.total_seconds()
        return 0.0

    @property
    def records_per_second(self) -> float:
        """Calculate throughput in records per second.

        Returns:
            Records per second, or 0.0 if duration is not available
        """
        if self.duration and self.duration.total_seconds() > 0:
            return self.records_processed / self.duration.total_seconds()
        return 0.0

    @property
    def error_rate(self) -> float:
        """Calculate error rate as percentage of total records.

        Returns:
            Error rate as percentage (0.0-100.0)
        """
        if self.records_processed > 0:
            total_errors = self.parse_errors + self.validation_errors + self.persistence_errors
            return (total_errors / self.records_processed) * 100
        return 0.0

    @property
    def total_errors(self) -> int:
        """Calculate total number of errors.

        Returns:
            Sum of all error counts
        """
        return self.parse_errors + self.validation_errors + self.persistence_errors

    @property
    def success_rate(self) -> float:
        """Calculate success rate as percentage of total records.

        Returns:
            Success rate as percentage (0.0-100.0)
        """
        if self.records_processed > 0:
            return (self.records_inserted / self.records_processed) * 100
        return 0.0
