import tempfile
from pathlib import Path

from loguru import logger

from ...file_extraction.utils.chart_extraction import (
    evaluate_excel_formulas_with_libreoffice,
)
from ..models import TransformationOutput
from ..spreadsheet_xml import spreadsheet_to_xml


async def spreadsheet_to_text(
    file_bytes: bytes, file_name: str
) -> TransformationOutput:
    suffix = Path(file_name).suffix
    recalc_bytes = await evaluate_excel_formulas_with_libreoffice(
        file_bytes, suffix=suffix
    )
    effective_bytes = recalc_bytes or file_bytes
    if not recalc_bytes:
        logger.debug("[TRANSFORM] LO recalculation unavailable, using cached values")

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(effective_bytes)
        tmp_path = Path(f.name)

    try:
        text = spreadsheet_to_xml(tmp_path, data_only=True)
        return TransformationOutput(text=text)
    finally:
        tmp_path.unlink(missing_ok=True)
