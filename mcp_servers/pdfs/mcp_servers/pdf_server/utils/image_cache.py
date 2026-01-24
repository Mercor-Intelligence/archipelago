"""In-memory cache for compressed images from PDFs.

This module provides a shared cache for storing images extracted from PDF documents.
Images are compressed once during extraction and stored as base64-encoded JPEG data.
"""

IMAGE_CACHE: dict[str, str] = {}

MAX_IMAGE_WIDTH = 1024
MAX_IMAGE_HEIGHT = 1024
IMAGE_QUALITY = 85  # JPEG quality (1-100)
