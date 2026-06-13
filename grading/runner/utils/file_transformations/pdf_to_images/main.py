import tempfile
from pathlib import Path

from ...file_extraction.utils.chart_extraction import pdf_to_base64_images
from ..models import TransformationOutput


async def pdf_to_images(file_bytes: bytes, file_name: str) -> TransformationOutput:
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(file_bytes)
        tmp_path = Path(f.name)
    try:
        images = pdf_to_base64_images(tmp_path)
        return TransformationOutput(images=images)
    finally:
        tmp_path.unlink(missing_ok=True)
