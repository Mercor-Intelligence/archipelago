"""
File extraction service - unified interface for extracting content from various document types.

Supports multiple extraction methods (Reducto, etc.) with a unified API.
"""

from .base import BaseFileExtractor, FileExtractor
from .factory import (
    FileExtractionService,
    RemoteParseUnavailableError,
    remote_parse_required,
)
from .types import ExtractedContent, ImageMetadata

__all__ = [
    "BaseFileExtractor",
    "FileExtractor",
    "FileExtractionService",
    "RemoteParseUnavailableError",
    "remote_parse_required",
    "ExtractedContent",
    "ImageMetadata",
]
