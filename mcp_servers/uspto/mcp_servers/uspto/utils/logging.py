"""Structured logging utilities for the USPTO MCP Server.

Provides comprehensive logging infrastructure with:
- JSON-formatted production logs compatible with ELK, Datadog
- Structured fields for querying and aggregation
- PII protection (no user_id, no API keys)
- Session-scoped correlation via request IDs
"""

from __future__ import annotations

import json
import re
import sys
import uuid
from typing import Any

from loguru import logger

SENSITIVE_SUBSTRINGS = ("key", "secret", "token", "password", "credential", "auth")

# Regex patterns to convert camelCase to snake_case (handles SCREAMING_SNAKE_CASE too)
# Pattern 1: Handle "XMLParser" → "XML_Parser" (uppercase sequence followed by uppercase+lowercase)
_CAMEL_PATTERN_1 = re.compile(r"([A-Z]+)([A-Z][a-z])")
# Pattern 2: Handle "apiKey" → "api_Key" (lowercase followed by uppercase)
_CAMEL_PATTERN_2 = re.compile(r"([a-z])([A-Z])")

NON_SENSITIVE_FIELDS = frozenset(
    {
        # Fields containing "key" that are not sensitive
        "primary_key",
        "foreign_key",
        "unique_key",
        "sort_key",
        "partition_key",
        "cache_key",
        "lookup_key",
        "index_key",
        "keyboard",
        "keynote",
        # Fields containing "auth" that are not sensitive
        "author",
        "authors",
        "author_id",
        "author_name",
        "authored_by",
        "authority",
        "authorities",
    }
)

REDACTED = "[REDACTED]"


def _normalize_field_name(key: str) -> str:
    """Normalize field name to snake_case for comparison.

    Handles camelCase, PascalCase, SCREAMING_SNAKE_CASE, and kebab-case.
    Examples:
        - "apiKey" → "api_key"
        - "API_KEY" → "api_key"
        - "XMLParser" → "xml_parser"
        - "api-key" → "api_key"
    """
    # Step 1: Handle uppercase sequences followed by lowercase (XMLParser → XML_Parser)
    result = _CAMEL_PATTERN_1.sub(r"\1_\2", key)
    # Step 2: Handle lowercase followed by uppercase (apiKey → api_Key)
    result = _CAMEL_PATTERN_2.sub(r"\1_\2", result)
    # Step 3: Lowercase and normalize hyphens to underscores
    return result.lower().replace("-", "_")


def _is_sensitive_key(key: str) -> bool:
    """Check if a key name contains sensitive substrings.

    Returns True if the field should be redacted from logs.
    Implements allowlist for known non-sensitive fields.
    """
    normalized = _normalize_field_name(key)
    # Skip known non-sensitive fields
    if normalized in NON_SENSITIVE_FIELDS:
        return False
    # If field name contains any sensitive substring, redact it
    return any(substr in normalized for substr in SENSITIVE_SUBSTRINGS)


def redact_sensitive_data(data: dict[str, Any]) -> dict[str, Any]:
    """Redact sensitive fields before logging.

    API keys and other credentials must NEVER appear in logs.
    Recursively processes nested dictionaries and lists.

    Args:
        data: Dictionary potentially containing sensitive data

    Returns:
        Dictionary with sensitive fields replaced with [REDACTED]
    """
    redacted: dict[str, Any] = {}
    for key, value in data.items():
        if _is_sensitive_key(key):
            redacted[key] = REDACTED
        elif isinstance(value, dict):
            redacted[key] = redact_sensitive_data(value)
        elif isinstance(value, list):
            redacted[key] = [
                redact_sensitive_data(item) if isinstance(item, dict) else item for item in value
            ]
        else:
            redacted[key] = value
    return redacted


def _json_serializer(record: dict[str, Any]) -> str:
    """Custom JSON serializer for structured logging.

    Formats log records as JSON with structured fields suitable
    for log aggregators (ELK, Datadog, Splunk).
    """
    # Extract extra fields (our structured data)
    # Loguru stores extra={...} as record["extra"]["extra"], so check for nested structure
    extra = record.get("extra", {})
    if isinstance(extra, dict) and "extra" in extra and isinstance(extra["extra"], dict):
        # Unwrap the nested extra dict that Loguru creates
        extra = extra["extra"]

    # Ensure extra is always a dict to prevent AttributeError on .items()
    if not isinstance(extra, dict):
        extra = {}

    # Standard fields that must be protected from overwriting
    standard_fields = {
        "timestamp",
        "level",
        "logger",
        "message",
        "module",
        "function",
        "line",
        "exception",
    }

    # Build structured log record with standard fields
    log_record = {
        "timestamp": record["time"].isoformat(),
        "level": record["level"].name,
        "logger": record["name"],
        "message": record["message"],
        "module": record["module"],
        "function": record["function"],
        "line": record["line"],
    }

    # Merge extra fields (request_id, tool, params, etc.)
    # Filter out any extra fields that would overwrite standard fields
    safe_extra = {k: v for k, v in extra.items() if k not in standard_fields}
    log_record.update(safe_extra)

    # Add exception info if present
    if record.get("exception"):
        exception = record["exception"]
        log_record["exception"] = {
            "type": exception.type.__name__ if exception.type else None,
            "value": str(exception.value) if exception.value else None,
            "traceback": exception.traceback if exception.traceback else None,
        }

    # Return JSON Lines format (newline-delimited JSON) for log aggregators
    return json.dumps(log_record, default=str) + "\n"


def configure_logging(
    log_level: str = "INFO",
    json_output: bool = False,
) -> None:
    """Configure structured logging for the USPTO MCP Server.

    Args:
        log_level: Minimum log level (DEBUG, INFO, WARNING, ERROR)
        json_output: If True, output JSON logs for production aggregators

    Production mode (json_output=True):
        - JSON-formatted logs with structured fields
        - Compatible with ELK, Datadog, Splunk
        - Machine-readable for aggregation and querying

    Development mode (json_output=False):
        - Human-readable colored output
        - Formatted for terminal viewing
    """
    logger.remove()

    if json_output:
        # Production: JSON logs for aggregators
        # Use a sink function instead of format to avoid Loguru treating JSON braces as placeholders
        def json_sink(message):
            """Custom sink that writes JSON-formatted log entries directly."""
            record = message.record
            log_entry = _json_serializer(record)
            sys.stderr.write(log_entry)
            sys.stderr.flush()

        logger.add(
            json_sink,
            level=log_level.upper(),
            colorize=False,
        )
    else:
        # Development: Human-readable colored logs
        logger.add(
            sys.stderr,
            format=(
                "<green>{time:HH:mm:ss}</green> | "
                "<level>{level: <8}</level> | "
                "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
                "<level>{message}</level>"
            ),
            level=log_level.upper(),
            colorize=True,
        )

    logger.info(f"Logging configured: level={log_level.upper()}, json_output={json_output}")


def generate_request_id() -> str:
    """Generate a unique request ID for tracing.

    Returns a UUID v4 for correlating logs across a request lifecycle.
    Used for session-scoped request correlation (not user tracking).
    """
    return str(uuid.uuid4())


def log_metric(
    metric_name: str,
    value: float | int,
    tags: dict[str, str] | None = None,
) -> None:
    """Log a metric in structured format for aggregation.

    Args:
        metric_name: Name of the metric (e.g., "response_time_ms")
        value: Numeric value of the metric
        tags: Optional tags for metric dimensions (e.g., {"tool": "search"})

    Example:
        log_metric("cache_hit_rate", 85.5, {"tool": "search"})
    """
    logger.info(
        f"Metric: {metric_name}",
        extra={
            "metric": metric_name,
            "value": value,
            "tags": tags or {},
        },
    )


__all__ = [
    "REDACTED",
    "configure_logging",
    "generate_request_id",
    "log_metric",
    "redact_sensitive_data",
]
