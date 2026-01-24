import os
from typing import Annotated

from models.response import DeleteSpreadsheetResponse
from pydantic import Field
from utils.decorators import make_async_background

SHEETS_ROOT = os.getenv("APP_SHEETS_ROOT") or os.getenv("APP_FS_ROOT", "/filesystem")


def _resolve_under_root(path: str) -> str:
    """Map path to the sheets root."""
    path = path.lstrip("/")
    full_path = os.path.join(SHEETS_ROOT, path)
    return os.path.normpath(full_path)


@make_async_background
def delete_spreadsheet(
    file_path: Annotated[str, Field(description="Path to the .xlsx file to delete")],
) -> str:
    """Delete the specified spreadsheet."""

    if not isinstance(file_path, str) or not file_path:
        return "File path is required"
    if not file_path.startswith("/"):
        return "File path must start with /"
    if not file_path.lower().endswith(".xlsx"):
        return "File path must end with .xlsx"

    target_path = _resolve_under_root(file_path)

    try:
        if os.path.exists(target_path):
            os.remove(target_path)
    except Exception as exc:
        return f"Failed to delete spreadsheet: {repr(exc)}"

    response = DeleteSpreadsheetResponse(
        status="success",
        file_path=file_path,
    )
    return str(response)
