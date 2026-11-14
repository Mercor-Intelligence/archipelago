"""Data source implementations.

This module provides concrete implementations of the DataSource interface
for common data sources like files, streams, and APIs.
"""

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any, BinaryIO

from .exceptions import SourceError
from .interfaces import DataSource


class FileSource(DataSource):
    """File-based data source.

    Reads data from files. Supports resume from byte offset.

    Args:
        file_path: Path to the file to read
        encoding: File encoding (default: None for binary mode)

    Example:
        >>> source = FileSource('/data/large_file.xml')
        >>> for data in source.stream():
        ...     process(data)
        >>> source.close()
    """

    def __init__(
        self,
        file_path: str | Path,
        encoding: str | None = None,
    ):
        """Initialize FileSource.

        Args:
            file_path: Path to the file to read
            encoding: File encoding (default: None for binary mode)

        Raises:
            SourceError: If file does not exist or is not readable
        """
        self.file_path = Path(file_path)
        self.encoding = encoding
        self._file_handle = None
        self._validate_file()

    def _validate_file(self) -> None:
        """Validate that file exists and is readable.

        Raises:
            SourceError: If file does not exist or is not readable
        """
        if not self.file_path.exists():
            raise SourceError(f"File not found: {self.file_path}")

        if not self.file_path.is_file():
            raise SourceError(f"Path is not a file: {self.file_path}")

        if not os.access(self.file_path, os.R_OK):
            raise SourceError(f"Permission denied: {self.file_path}")

    def stream(self, start_position: Any = None) -> Iterator[BinaryIO]:
        """Stream file handle.

        Args:
            start_position: Byte offset to start reading from (for resume)

        Yields:
            File handle (BinaryIO) opened in binary read mode

        Raises:
            SourceError: If file cannot be opened

        Note:
            File handle remains open until close() is called.
            Caller is responsible for calling close() after extraction.
        """
        # Validate start_position before opening file
        if start_position is not None:
            # Reject booleans explicitly (bool is subclass of int in Python)
            if isinstance(start_position, bool) or not isinstance(start_position, int):
                raise SourceError(
                    f"start_position must be int (byte offset), got {type(start_position).__name__}"
                )

        try:
            # Open file in binary mode
            mode = "rb"
            self._file_handle = open(self.file_path, mode)

            # Seek to start position if resuming
            if start_position is not None:
                self._file_handle.seek(start_position)

            # Yield the file handle for streaming extraction
            # File handle stays open until close() is called
            yield self._file_handle

        except OSError as e:
            # Close file handle if it was opened
            if self._file_handle:
                self._file_handle.close()
                self._file_handle = None
            raise SourceError(f"Error opening file {self.file_path}: {e}") from e

    def get_metadata(self) -> dict[str, Any]:
        """Get file metadata.

        Returns:
            Dictionary with file information (path, size, type, etc.)
        """
        stat = self.file_path.stat()
        return {
            "type": "file",
            "path": str(self.file_path.absolute()),
            "size_bytes": stat.st_size,
            "modified_time": stat.st_mtime,
            "encoding": self.encoding,
            "supports_resume": True,
        }

    def supports_resume(self) -> bool:
        """Check if source supports resume from a position.

        Returns:
            True (files always support resume via byte offset)
        """
        return True

    def close(self) -> None:
        """Cleanup and release file handle.

        Safe to call multiple times.
        """
        if self._file_handle:
            self._file_handle.close()
            self._file_handle = None
