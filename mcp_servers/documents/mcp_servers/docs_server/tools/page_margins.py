"""Page Margins tool for reading and modifying document page margins."""

import os

from docx import Document
from docx.shared import Inches
from utils.decorators import make_async_background
from utils.models import PageMarginsReadResponse, PageMarginsSetResponse

DOCS_ROOT = os.getenv("APP_DOCS_ROOT") or os.getenv("APP_FS_ROOT", "/filesystem")


def _resolve_under_root(path: str) -> str:
    """Map path to the docs root."""
    path = path.lstrip("/")
    full_path = os.path.join(DOCS_ROOT, path)
    return os.path.normpath(full_path)


def _emu_to_inches(emu: int | None) -> float | None:
    """Convert EMUs (English Metric Units) to inches."""
    if emu is None:
        return None
    # 1 inch = 914400 EMUs
    return round(emu / 914400, 4)


@make_async_background
def page_margins(
    file_path: str,
    action: str,
    section_index: int = 0,
    top: float | None = None,
    bottom: float | None = None,
    left: float | None = None,
    right: float | None = None,
) -> str:
    """Read and modify page margins in Documents documents.

    Args:
        file_path: Path to the .docx file (must start with /)
        action: Action to perform: "read" or "set"
        section_index: Section index (0-based, defaults to 0)
        top: Top margin in inches (for "set" action, 0-22 inches)
        bottom: Bottom margin in inches (for "set" action, 0-22 inches)
        left: Left margin in inches (for "set" action, 0-22 inches)
        right: Right margin in inches (for "set" action, 0-22 inches)

    Returns:
        String representation of the result or error message

    Note:
        Zero margins are allowed but may cause content to be cut off when printing.
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

    # Validate set action has at least one margin and all margins are valid
    # Max margin of 22 inches (larger than any standard paper size)
    MAX_MARGIN_INCHES = 22.0
    if action == "set":
        if top is None and bottom is None and left is None and right is None:
            return "At least one margin (top, bottom, left, right) is required for 'set' action"
        for name, value in [
            ("top", top),
            ("bottom", bottom),
            ("left", left),
            ("right", right),
        ]:
            if value is not None:
                if value < 0:
                    return f"Margin '{name}' cannot be negative: {value}"
                if value > MAX_MARGIN_INCHES:
                    return f"Margin '{name}' exceeds maximum ({MAX_MARGIN_INCHES} inches): {value}"

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
        result = PageMarginsReadResponse(
            filepath=file_path,
            status="success",
            section_index=section_index,
            top=_emu_to_inches(section.top_margin),
            bottom=_emu_to_inches(section.bottom_margin),
            left=_emu_to_inches(section.left_margin),
            right=_emu_to_inches(section.right_margin),
        )
        return str(result)

    elif action == "set":
        old_top = _emu_to_inches(section.top_margin)
        old_bottom = _emu_to_inches(section.bottom_margin)
        old_left = _emu_to_inches(section.left_margin)
        old_right = _emu_to_inches(section.right_margin)

        # Apply margins
        if top is not None:
            section.top_margin = Inches(top)
        if bottom is not None:
            section.bottom_margin = Inches(bottom)
        if left is not None:
            section.left_margin = Inches(left)
        if right is not None:
            section.right_margin = Inches(right)

        # Save document
        try:
            doc.save(target_path)
        except Exception as exc:
            return f"Failed to save document: {repr(exc)}"

        result = PageMarginsSetResponse(
            filepath=file_path,
            status="success",
            section_index=section_index,
            old_top=old_top,
            old_bottom=old_bottom,
            old_left=old_left,
            old_right=old_right,
            new_top=top if top is not None else old_top,
            new_bottom=bottom if bottom is not None else old_bottom,
            new_left=left if left is not None else old_left,
            new_right=right if right is not None else old_right,
        )
        return str(result)

    else:
        return f"Unknown action: {action}"
