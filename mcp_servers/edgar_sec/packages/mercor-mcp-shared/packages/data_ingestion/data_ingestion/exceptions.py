"""Exception hierarchy for data ingestion framework.

This module defines a hierarchy of exceptions that categorize different
types of errors that can occur during ingestion, with appropriate handling
strategies for each category.
"""


class IngestionError(Exception):
    """Base exception for all ingestion-related errors.

    All framework exceptions inherit from this base class.
    """

    pass


class SourceError(IngestionError):
    """Source-related errors (critical - stop processing).

    Raised when the data source cannot be accessed or read.
    Examples:
    - File not found
    - API authentication failed
    - Network unreachable
    - Insufficient permissions

    Handling: Stop processing immediately, report error to user.
    """

    pass


class ExtractionError(IngestionError):
    """Extraction errors (record-level - skip and continue).

    Raised when a specific record cannot be parsed or extracted.
    Examples:
    - Malformed XML/JSON
    - Invalid data format
    - Unexpected structure

    Handling: Log error, increment error counter, skip record, continue processing.
    """

    pass


class ValidationError(IngestionError):
    """Validation errors (record-level - skip and continue).

    Raised when a record fails business validation rules.
    Examples:
    - Missing required field
    - Invalid date format
    - Out of range value
    - Type mismatch

    Handling: Log error, increment error counter, skip record, continue processing.
    """

    pass


class PersistenceError(IngestionError):
    """Persistence errors (batch-level - retry or skip).

    Raised when data cannot be saved to the persistence layer.
    Examples:
    - Database connection lost
    - Constraint violation
    - Disk full
    - Transaction deadlock

    Handling: Retry batch with backoff, or skip and log.
    """

    pass


class ConfigurationError(IngestionError):
    """Configuration errors (critical - stop processing).

    Raised when configuration is invalid or missing.
    Examples:
    - Missing required config section
    - Invalid YAML syntax
    - Invalid XPath expression
    - Unknown source/extractor type

    Handling: Stop processing immediately, report configuration issue to user.
    """

    pass
