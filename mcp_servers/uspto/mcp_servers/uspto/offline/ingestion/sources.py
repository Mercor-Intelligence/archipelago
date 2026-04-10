"""USPTO-specific data sources for bulk file ingestion."""

import logging
import os
from collections.abc import Iterator
from io import BytesIO
from pathlib import Path
from typing import Any, BinaryIO

from data_ingestion.exceptions import SourceError
from data_ingestion.interfaces import DataSource

logger = logging.getLogger(__name__)


class USPTOBulkFileSource(DataSource):
    """Source for USPTO bulk XML files with concatenated documents.

    USPTO bulk data files contain multiple XML documents concatenated together,
    each with its own <?xml> declaration and <!DOCTYPE>. This source splits
    the file on XML declaration boundaries and yields each document separately.

    Args:
        file_path: Path to the USPTO bulk XML file

    Example:
        >>> source = USPTOBulkFileSource('/data/patent-grant/ipg251230.xml')
        >>> for xml_doc in source.stream():
        ...     # Each xml_doc is a BytesIO containing one complete XML document
        ...     process(xml_doc)
        >>> source.close()
    """

    def __init__(self, file_path: str | Path):
        """Initialize USPTOBulkFileSource.

        Args:
            file_path: Path to the USPTO bulk XML file

        Raises:
            SourceError: If file does not exist or is not readable
        """
        self.file_path = Path(file_path)
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
        """Stream individual XML documents from concatenated bulk file.

        Reads the bulk file line-by-line, detecting XML declaration boundaries
        to split documents. Each complete document is accumulated in a BytesIO
        buffer and yielded as a file handle.

        Args:
            start_position: Not supported for bulk files (reserved for future use)

        Yields:
            BytesIO file handles, each containing one complete XML document

        Raises:
            SourceError: If file cannot be read

        Note:
            Memory usage is constant - only one document buffered at a time.
            The bulk file is never loaded entirely into memory.
        """
        if start_position is not None:
            logger.warning("start_position not supported for USPTOBulkFileSource, ignoring")

        try:
            with open(self.file_path, "rb") as bulk_file:
                current_doc = BytesIO()
                document_count = 0

                for line in bulk_file:
                    # Detect start of new document (check if line contains <?xml)
                    # Some files have closing tag and <?xml on same line: </tag><?xml...
                    xml_decl_pos = line.find(b"<?xml")
                    if xml_decl_pos >= 0:
                        # If we have accumulated a previous document, yield it
                        if current_doc.tell() > 0:
                            # Write the part before <?xml to current document
                            if xml_decl_pos > 0:
                                current_doc.write(line[:xml_decl_pos])
                            current_doc.seek(0)
                            yield current_doc
                            document_count += 1

                            # Start new buffer for next document
                            current_doc = BytesIO()
                            # Write the part from <?xml onwards to new document
                            current_doc.write(line[xml_decl_pos:])
                        else:
                            # First document in file
                            current_doc.write(line)
                    else:
                        # No <?xml in this line, add to current document
                        current_doc.write(line)

                # Yield the last document
                if current_doc.tell() > 0:
                    current_doc.seek(0)
                    yield current_doc
                    document_count += 1

                logger.info(f"Streamed {document_count} XML documents from {self.file_path.name}")

        except OSError as e:
            raise SourceError(f"Error reading bulk file {self.file_path}: {e}") from e

    def get_metadata(self) -> dict[str, Any]:
        """Get bulk file metadata.

        Returns:
            Dictionary with file information
        """
        stat = self.file_path.stat()
        return {
            "type": "uspto_bulk",
            "path": str(self.file_path.absolute()),
            "size_bytes": stat.st_size,
            "modified_time": stat.st_mtime,
            "supports_resume": False,  # Not yet implemented for bulk files
            "format": "concatenated_xml",
        }

    def supports_resume(self) -> bool:
        """Check if source supports resume from a position.

        Returns:
            False (resume not yet implemented for bulk files)
        """
        return False

    def close(self) -> None:
        """Cleanup resources.

        No cleanup needed for bulk file source (file is opened/closed per stream()).
        """
        pass
