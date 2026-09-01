"""
Base interface for file extraction methods.
"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Protocol

from .types import ExtractedContent


class FileExtractor(Protocol):
    """Protocol for file extraction implementations"""

    @abstractmethod
    async def extract_from_file(
        self,
        file_path: Path,
        *,
        include_images: bool = True,
        sub_artifact_index: int | None = None,
    ) -> ExtractedContent:
        """
        Extract text and optionally images from a file.

        Args:
            file_path: Path to the file to extract content from
            include_images: Whether to extract and include images
            sub_artifact_index: Optional 0-based index of specific sub-artifact to extract
                              (only applicable for multi-part documents like presentations/spreadsheets)

        Returns:
            ExtractedContent with text and optional images
        """
        ...

    @abstractmethod
    def supports_file_type(self, file_extension: str) -> bool:
        """
        Check if this extractor supports a given file type.

        Args:
            file_extension: File extension (e.g., '.pdf', '.docx')

        Returns:
            True if this extractor can handle this file type
        """
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Name of the extraction method"""
        ...


class BaseFileExtractor(ABC):
    """Abstract base class for file extractors"""

    @abstractmethod
    async def extract_from_file(
        self,
        file_path: Path,
        *,
        include_images: bool = True,
        sub_artifact_index: int | None = None,
    ) -> ExtractedContent:
        """
        Extract content from a file.

        Args:
            file_path: Path to the file to extract content from
            include_images: Whether to extract and include images
            sub_artifact_index: Optional 0-based index of specific sub-artifact to extract
                              (only applicable for multi-part documents, can be ignored by simple extractors)
        """
        pass

    @abstractmethod
    def supports_file_type(self, file_extension: str) -> bool:
        """Check if this extractor supports a file type"""
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        """Name of the extraction method"""
        pass
