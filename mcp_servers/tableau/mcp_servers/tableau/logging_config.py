"""Logging configuration for tableau MCP server.

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


def get_logger(name: str) -> logging.Logger:
    """Get a configured logger instance.

    Args:
        name: The logger name (typically __name__ from the calling module)

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

    # Create formatters
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

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

    # Prevent propagation to root logger to avoid duplicate messages
    logger.propagate = False

    return logger
