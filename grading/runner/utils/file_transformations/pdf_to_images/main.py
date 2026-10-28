import asyncio
import tempfile
from pathlib import Path

from ...file_extraction.utils.chart_extraction import (
    DEFAULT_MAX_PAGES,
    pdf_to_base64_images,
)
from ..models import TransformationOutput


async def pdf_to_images(
    file_bytes: bytes, file_name: str, /, *, max_pages: int = DEFAULT_MAX_PAGES
) -> TransformationOutput:
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(file_bytes)
        tmp_path = Path(f.name)
    try:
        images = await asyncio.to_thread(pdf_to_base64_images, tmp_path, max_pages)
        return TransformationOutput(images=images)
    finally:
        tmp_path.unlink(missing_ok=True)
