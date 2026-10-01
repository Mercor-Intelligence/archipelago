import tempfile
from pathlib import Path

from ..models import TransformationOutput
from ..spreadsheet_xml import spreadsheet_to_xml


async def spreadsheet_to_text_formulas(
    file_bytes: bytes, file_name: str
) -> TransformationOutput:
    with tempfile.NamedTemporaryFile(suffix=Path(file_name).suffix, delete=False) as f:
        f.write(file_bytes)
        tmp_path = Path(f.name)

    try:
        text = spreadsheet_to_xml(tmp_path, data_only=False)
        return TransformationOutput(text=text)
    finally:
        tmp_path.unlink(missing_ok=True)
