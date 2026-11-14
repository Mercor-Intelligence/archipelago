"""Logging configuration for scripts.

Provides a configured logger with:
- Log level controlled by LOGLEVEL environment variable (default: INFO)
- DEBUG and INFO logs go to stdout (for backwards compatibility)
- WARNING, ERROR, and CRITICAL logs go to stderr
"""

import logging
import os
import sys


class StdoutFilter(logging.Filter):
    """Filter that only allows DEBUG and INFO level records."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno < logging.WARNING


class StderrFilter(logging.Filter):
    """Filter that only allows WARNING and above level records."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= logging.WARNING


def _configure_handlers(logger: logging.Logger, stderr_only: bool = False) -> None:
    """Configure handlers for a logger.

    Args:
        logger: The logger to configure
        stderr_only: If True, send all log output to stderr
    """
    # Clear existing handlers
    logger.handlers.clear()

    # Create formatter
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if stderr_only:
        # Single stderr handler for all log levels
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setLevel(logging.DEBUG)
        stderr_handler.setFormatter(formatter)
        logger.addHandler(stderr_handler)
    else:
        # Stdout handler for DEBUG and INFO
        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.setLevel(logging.DEBUG)
        stdout_handler.addFilter(StdoutFilter())
        stdout_handler.setFormatter(formatter)

        # Stderr handler for WARNING, ERROR, CRITICAL
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setLevel(logging.WARNING)
        stderr_handler.addFilter(StderrFilter())
        stderr_handler.setFormatter(formatter)

        logger.addHandler(stdout_handler)
        logger.addHandler(stderr_handler)


def reconfigure_logger(logger: logging.Logger, stderr_only: bool = False) -> None:
    """Reconfigure an existing logger's handlers.

    Use this to change a logger's output destination after initialization,
    for example to switch to stderr-only mode when --json flag is detected.

    Args:
        logger: The logger to reconfigure
        stderr_only: If True, send all log output to stderr (useful for
            scripts that output structured data like JSON to stdout)
    """
    _configure_handlers(logger, stderr_only)


def get_logger(name: str, stderr_only: bool = False) -> logging.Logger:
    """Get a configured logger instance.

    Args:
        name: The logger name (typically __name__ from the calling module)
        stderr_only: If True, send all log output to stderr (useful for
            scripts that output structured data like JSON to stdout)

    Returns:
        A configured logger with stdout/stderr handlers based on log level
    """
    logger = logging.getLogger(name)

    # Avoid adding duplicate handlers if logger already configured
    if logger.handlers:
        return logger

    # Get log level from environment variable, default to INFO
    log_level_str = os.environ.get("LOGLEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_str, logging.INFO)

    logger.setLevel(log_level)

    _configure_handlers(logger, stderr_only)

    # Prevent propagation to root logger to avoid duplicate messages
    logger.propagate = False

    return logger
