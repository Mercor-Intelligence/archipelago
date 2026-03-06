import tempfile
from pathlib import Path

from ...file_extraction.factory import FileExtractionService
from ..models import TransformationOutput


async def docx_to_text(file_bytes: bytes, file_name: str) -> TransformationOutput:
    service = FileExtractionService()
    with tempfile.NamedTemporaryFile(suffix=Path(file_name).suffix, delete=False) as f:
        f.write(file_bytes)
        tmp_path = Path(f.name)
    try:
        result = await service.extract_from_file(tmp_path, include_images=False)
        return TransformationOutput(text=result.text if result else None)
    finally:
        tmp_path.unlink(missing_ok=True)
