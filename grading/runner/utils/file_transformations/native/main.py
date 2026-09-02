import tempfile
from pathlib import Path

from ...file_extraction.factory import FileExtractionService
from ..models import TransformationOutput


async def native_extraction(file_bytes: bytes, file_name: str) -> TransformationOutput:
    service = FileExtractionService()
    with tempfile.NamedTemporaryFile(suffix=Path(file_name).suffix, delete=False) as f:
        f.write(file_bytes)
        tmp_path = Path(f.name)
    try:
        result = await service.extract_from_file(tmp_path, include_images=True)
        if not result:
            return TransformationOutput()
        return TransformationOutput(text=result.text, images=list(result.images))
    finally:
        tmp_path.unlink(missing_ok=True)
