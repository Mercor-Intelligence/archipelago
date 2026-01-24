import os
import shutil
import subprocess
import tempfile
from typing import Any

from loguru import logger


def recalculate_formulas(file_path: str) -> None:
    """
    Recalculate formulas in an Spreadsheets file using LibreOffice in headless mode.

    This function uses LibreOffice to open and re-save the file, which triggers
    formula recalculation. The recalculated values are then cached in the file
    and can be read by openpyxl with data_only=True.

    Args:
        file_path: Absolute path to the Spreadsheets file

    Notes:
        - Silently returns if LibreOffice is not available
        - Logs errors but does not raise exceptions (graceful degradation)
        - Requires 'soffice' command to be available in PATH
    """
    if not shutil.which("soffice"):
        logger.debug(
            "LibreOffice (soffice) not found in PATH, skipping formula recalculation"
        )
        return

    timeout = int(os.getenv("LIBREOFFICE_TIMEOUT", 30))
    try:
        abs_path = os.path.abspath(file_path)

        with tempfile.TemporaryDirectory() as temp_dir:
            result = subprocess.run(
                [
                    "soffice",
                    "--headless",
                    "--calc",
                    "--convert-to",
                    "xlsx",
                    "--infilter=Calc MS Spreadsheets 2007 XML",
                    "--outdir",
                    temp_dir,
                    abs_path,
                ],
                capture_output=True,
                timeout=timeout,
                check=False,
            )

            if result.returncode != 0:
                logger.warning(
                    f"LibreOffice formula recalculation failed (exit code {result.returncode}): "
                    f"stdout={result.stdout.decode('utf-8', errors='ignore')}, "
                    f"stderr={result.stderr.decode('utf-8', errors='ignore')}"
                )
                return

            filename = os.path.basename(abs_path)
            temp_file = os.path.join(temp_dir, filename)

            if os.path.exists(temp_file):
                os.replace(temp_file, abs_path)
                logger.debug(f"Successfully recalculated formulas in {file_path}")
            else:
                logger.warning(
                    f"LibreOffice did not create expected output file: {temp_file}"
                )

    except subprocess.TimeoutExpired:
        logger.warning(
            f"LibreOffice formula recalculation timed out for {file_path} "
            f"(timeout: {timeout}s). For large spreadsheets, increase LIBREOFFICE_TIMEOUT"
        )
    except Exception as exc:
        logger.warning(
            f"LibreOffice formula recalculation failed for {file_path}: {exc}"
        )


def trim_empty_trailing_data(values: list[list[Any]]) -> list[list[Any]]:
    """Trim empty trailing rows and columns from sheet data.

    This removes rows that are completely None after the last row with data,
    and columns that are completely None after the last column with data.

    Args:
        values: 2D list of cell values

    Returns:
        Trimmed values
    """
    if not values:
        return values

    # Remove trailing empty rows
    while values and all(cell is None for cell in values[-1]):
        values.pop()

    # Remove trailing empty columns from each row
    if values:
        max_col = max(
            max((i for i, cell in enumerate(row) if cell is not None), default=-1)
            for row in values
        )
        if max_col >= 0:
            values = [row[: max_col + 1] for row in values]
        else:
            # All cells are None
            values = []

    return values
