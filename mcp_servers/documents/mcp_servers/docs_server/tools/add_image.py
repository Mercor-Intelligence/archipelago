import os
from typing import Any

from docx import Document
from docx.shared import Inches
from helpers.identifier import parse_identifier
from helpers.mutate import resolve_target
from utils.decorators import make_async_background

DOCS_ROOT = os.getenv("APP_DOCS_ROOT") or os.getenv("APP_FS_ROOT", "/filesystem")


def _resolve_under_root(path: str) -> str:
    """Map path to the docs root."""
    path = path.lstrip("/")
    full_path = os.path.join(DOCS_ROOT, path)
    return os.path.normpath(full_path)


@make_async_background
def add_image(
    file_path: str,
    image_path: str,
    identifier: str,
    position: str = "end",
    width: float | None = None,
    height: float | None = None,
) -> str:
    """Add an image to a document at the specified location."""
    if not isinstance(file_path, str) or not file_path:
        return "File path is required"
    if not file_path.startswith("/"):
        return "File path must start with /"
    if not file_path.lower().endswith(".docx"):
        return "File path must end with .docx"

    if not isinstance(image_path, str) or not image_path:
        return "Image path is required"
    if not image_path.startswith("/"):
        return "Image path must start with /"

    image_ext = image_path.lower().split(".")[-1]
    if image_ext not in ("jpg", "jpeg", "png"):
        return (
            f"Unsupported image format: {image_ext}. Supported formats: jpg, jpeg, png"
        )

    if position not in ("start", "end"):
        return f"Position must be 'start' or 'end', got: {position}"

    target_path = _resolve_under_root(file_path)

    if not os.path.exists(target_path):
        return f"File not found: {file_path}"

    try:
        doc = Document(target_path)
    except Exception as exc:
        return f"Failed to open document: {repr(exc)}"

    try:
        parsed = parse_identifier(identifier)
    except Exception as exc:
        return f"Invalid identifier: {repr(exc)}"

    try:
        target_kind, target_obj, target_type = resolve_target(doc, parsed)
    except Exception as exc:
        return f"Failed to resolve target: {repr(exc)}"

    if target_obj is None:
        return f"Target not found: {identifier}"

    if target_type not in ("paragraph", "run"):
        return f"Image insertion only supports paragraph or run targets, got: {target_type}"

    image_full_path = _resolve_under_root(image_path)

    if not os.path.exists(image_full_path):
        return f"Image file not found: {image_path}"

    try:
        target_paragraph: Any = None
        if target_type == "run":
            run_element = target_obj._element  # type: ignore[attr-defined]
            para_element = run_element.getparent()

            for p in doc.paragraphs:
                if p._element == para_element:
                    target_paragraph = p
                    break

            if target_paragraph is None:
                for table in doc.tables:
                    for row in table.rows:
                        for cell in row.cells:
                            for p in cell.paragraphs:
                                if p._element == para_element:
                                    target_paragraph = p
                                    break
                            if target_paragraph:
                                break
                        if target_paragraph:
                            break
                    if target_paragraph:
                        break

            if target_paragraph is None:
                return "Could not locate paragraph for run"
        else:
            target_paragraph = target_obj

        if position == "start":
            new_run = target_paragraph.insert_paragraph_before("")  # type: ignore[attr-defined]
            para_element = new_run._element  # type: ignore[attr-defined]
            target_paragraph._element.getparent().remove(para_element)  # type: ignore[attr-defined]
            new_run = target_paragraph.add_run()  # type: ignore[attr-defined]
            run_element = new_run._element  # type: ignore[attr-defined]
            target_paragraph._element.remove(run_element)  # type: ignore[attr-defined]
            para_children = list(target_paragraph._element)  # type: ignore[attr-defined]
            insert_position = 0
            if para_children and para_children[0].tag.endswith("}pPr"):
                insert_position = 1
            target_paragraph._element.insert(insert_position, run_element)  # type: ignore[attr-defined]
        else:  # end
            new_run = target_paragraph.add_run()  # type: ignore[attr-defined]

        if width is not None and height is not None:
            new_run.add_picture(  # type: ignore[attr-defined]
                image_full_path, width=Inches(width), height=Inches(height)
            )
        elif width is not None:
            new_run.add_picture(image_full_path, width=Inches(width))  # type: ignore[attr-defined]
        elif height is not None:
            new_run.add_picture(image_full_path, height=Inches(height))  # type: ignore[attr-defined]
        else:
            new_run.add_picture(image_full_path)  # type: ignore[attr-defined]

    except Exception as exc:
        return f"Failed to add image: {repr(exc)}"

    # Save document
    try:
        doc.save(target_path)
    except Exception as exc:
        return f"Failed to save document: {repr(exc)}"

    dims = f" ({width}x{height} in)" if width and height else ""
    return f"Image added to {identifier} at position {position}{dims}"
