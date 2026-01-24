"""Page Orientation tool for reading and modifying document page orientation."""

import os

from docx import Document
from docx.enum.section import WD_ORIENT
from utils.decorators import make_async_background
from utils.models import PageOrientationReadResponse, PageOrientationSetResponse

DOCS_ROOT = os.getenv("APP_DOCS_ROOT") or os.getenv("APP_FS_ROOT", "/filesystem")


def _resolve_under_root(path: str) -> str:
    """Map path to the docs root."""
    path = path.lstrip("/")
    full_path = os.path.join(DOCS_ROOT, path)
    return os.path.normpath(full_path)


def _orientation_to_str(orientation: WD_ORIENT | None) -> str:
    """Convert WD_ORIENT enum to string."""
    if orientation == WD_ORIENT.LANDSCAPE:
        return "landscape"
    return "portrait"


@make_async_background
def page_orientation(
    file_path: str,
    action: str,
    section_index: int = 0,
    orientation: str | None = None,
) -> str:
    """Read and modify page orientation in Documents documents.

    Args:
        file_path: Path to the .docx file (must start with /)
        action: Action to perform: "read" or "set"
        section_index: Section index (0-based, defaults to 0)
        orientation: For "set" action: "portrait" or "landscape"

    Returns:
        String representation of the result or error message
    """
    # Validate file_path
    if not isinstance(file_path, str) or not file_path:
        return "File path is required"
    if not file_path.startswith("/"):
        return "File path must start with /"
    if not file_path.lower().endswith(".docx"):
        return "File path must end with .docx"

    # Validate action
    valid_actions = {"read", "set"}
    if action not in valid_actions:
        return f"Invalid action: {action}. Must be one of: {', '.join(sorted(valid_actions))}"

    # Validate set action has orientation
    orientation_lower: str | None = None
    if action == "set":
        if orientation is None:
            return "Orientation is required for 'set' action"
        if not isinstance(orientation, str):
            return "Orientation must be a string"
        orientation_lower = orientation.lower()
        if orientation_lower not in {"portrait", "landscape"}:
            return (
                f"Invalid orientation: {orientation}. Must be 'portrait' or 'landscape'"
            )

    # Load document
    target_path = _resolve_under_root(file_path)

    try:
        if not os.path.exists(target_path):
            return f"File not found: {file_path}"
        if not os.path.isfile(target_path):
            return f"Not a file: {file_path}"

        doc = Document(target_path)
    except Exception as exc:
        return f"Failed to read document: {repr(exc)}"

    # Validate section_index
    if section_index < 0 or section_index >= len(doc.sections):
        return f"Invalid section_index: {section_index}. Document has {len(doc.sections)} sections."

    section = doc.sections[section_index]

    # Handle each action
    if action == "read":
        current_orientation = _orientation_to_str(section.orientation)
        width_inches = (
            round(section.page_width / 914400, 2) if section.page_width else None
        )
        height_inches = (
            round(section.page_height / 914400, 2) if section.page_height else None
        )

        result = PageOrientationReadResponse(
            filepath=file_path,
            status="success",
            section_index=section_index,
            orientation=current_orientation,
            page_width=width_inches,
            page_height=height_inches,
        )
        return str(result)

    elif action == "set":
        if orientation_lower is None:
            return "Orientation is required for 'set' action"
        old_orientation = _orientation_to_str(section.orientation)
        new_orientation = orientation_lower

        # Set orientation and swap dimensions if needed
        if new_orientation == "landscape":
            if section.orientation != WD_ORIENT.LANDSCAPE:
                # Swap width and height when changing to landscape
                new_width = section.page_height
                new_height = section.page_width
                section.orientation = WD_ORIENT.LANDSCAPE
                section.page_width = new_width
                section.page_height = new_height
        else:  # portrait
            if section.orientation != WD_ORIENT.PORTRAIT:
                # Swap width and height when changing to portrait
                new_width = section.page_height
                new_height = section.page_width
                section.orientation = WD_ORIENT.PORTRAIT
                section.page_width = new_width
                section.page_height = new_height

        # Save document
        try:
            doc.save(target_path)
        except Exception as exc:
            return f"Failed to save document: {repr(exc)}"

        result = PageOrientationSetResponse(
            filepath=file_path,
            status="success",
            section_index=section_index,
            old_orientation=old_orientation,
            new_orientation=new_orientation,
        )
        return str(result)

    else:
        return f"Unknown action: {action}"
