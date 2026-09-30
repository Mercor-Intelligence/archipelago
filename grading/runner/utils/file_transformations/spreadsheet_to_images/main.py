import asyncio
import shutil
import tempfile
from pathlib import Path

from loguru import logger

from ...file_extraction.utils.chart_extraction import (
    DEFAULT_MAX_PAGES,
    convert_xlsx_to_pdf,
    find_libreoffice,
    pdf_to_base64_images,
)
from ..models import TransformationOutput


async def spreadsheet_to_images(
    file_bytes: bytes, file_name: str, /, *, max_pages: int = DEFAULT_MAX_PAGES
) -> TransformationOutput:
    soffice_path = find_libreoffice()
    if not soffice_path:
        logger.warning(
            "[TRANSFORM] LibreOffice not found for spreadsheet-to-images conversion"
        )
        return TransformationOutput()

    with tempfile.NamedTemporaryFile(suffix=Path(file_name).suffix, delete=False) as f:
        f.write(file_bytes)
        tmp_path = Path(f.name)

    try:
        pdf_path = await convert_xlsx_to_pdf(tmp_path, soffice_path)
        if not pdf_path:
            return TransformationOutput()
        try:
            images = await asyncio.to_thread(pdf_to_base64_images, pdf_path, max_pages)
            return TransformationOutput(images=images)
        finally:
            if pdf_path.exists():
                pdf_path.unlink(missing_ok=True)
            if pdf_path.parent.exists():
                shutil.rmtree(pdf_path.parent, ignore_errors=True)
    finally:
        tmp_path.unlink(missing_ok=True)
