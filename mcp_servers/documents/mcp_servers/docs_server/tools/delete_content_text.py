import os

from docx import Document
from helpers.identifier import parse_identifier
from helpers.mutate import resolve_target
from utils.decorators import make_async_background
from utils.models import DeleteContentTextResponse, TargetInfo

DOCS_ROOT = os.getenv("APP_DOCS_ROOT") or os.getenv("APP_FS_ROOT", "/filesystem")


def _resolve_under_root(path: str) -> str:
    """Map path to the docs root."""
    path = path.lstrip("/")
    full_path = os.path.join(DOCS_ROOT, path)
    return os.path.normpath(full_path)


@make_async_background
def delete_content_text(
    file_path: str,
    identifier: str,
    scope: str = "content",
    collapse_whitespace: bool = False,
) -> str:
    """Delete text or remove elements by identifier."""

    if not isinstance(file_path, str) or not file_path:
        return "File path is required"
    if not file_path.startswith("/"):
        return "File path must start with /"
    if not file_path.lower().endswith(".docx"):
        return "File path must end with .docx"
    if not identifier or not identifier.strip():
        return "Identifier is required"

    target_path = _resolve_under_root(file_path)

    try:
        if not os.path.exists(target_path):
            return f"File not found: {file_path}"
        if not os.path.isfile(target_path):
            return f"Not a file: {file_path}"

        doc = Document(target_path)
    except Exception as exc:
        return f"Failed to read document: {repr(exc)}"

    # Resolve identifier and target object
    try:
        parsed = parse_identifier(identifier)
        target_kind, target_obj, target_type = resolve_target(doc, parsed)
    except Exception as exc:
        return f"Failed to parse identifier '{identifier}'. Please ensure the identifier is valid from read_document_content tool. Error: {repr(exc)}"

    # Validate scope
    sc = (scope or "content").strip().lower()
    if sc not in {"content", "element"}:
        return "Scope must be 'content' or 'element'"

    summary: dict = {"scope": sc, "target_kind": target_kind}

    # Perform deletion
    try:
        if target_type == "run":
            if sc == "content":
                summary["old_text"] = target_obj.text
                target_obj.text = ""
            else:
                # remove run element
                r_el = target_obj._element
                r_el.getparent().remove(r_el)
                summary["removed"] = True

        elif target_type == "paragraph":
            if sc == "content":
                # clear all runs
                texts = [r.text for r in target_obj.runs]
                summary["old_text_runs"] = texts
                for r in list(target_obj.runs):
                    r.text = ""
            else:
                # remove paragraph element
                p_el = target_obj._element
                p_el.getparent().remove(p_el)
                summary["removed"] = True

        elif target_type == "cell":
            # Deleting a cell element is not supported
            if sc == "element":
                return "Cell elements cannot be deleted. Use scope='content' to clear cell contents."

            # Clear cell contents
            texts = []
            for p in list(target_obj.paragraphs):
                texts.append(p.text)
                for r in list(p.runs):
                    r.text = ""
            summary["old_paragraph_texts"] = texts

            if collapse_whitespace and len(target_obj.paragraphs) > 1:
                # keep only first paragraph
                first = target_obj.paragraphs[0]
                for p in target_obj.paragraphs[1:]:
                    p_el = p._element
                    p_el.getparent().remove(p_el)
                # Ensure first paragraph has empty text
                if not first.runs:
                    first.add_run("")

        else:
            return f"Unsupported target for delete; use run, paragraph, or cell (got {target_type})"

    except Exception as exc:
        return f"Failed to delete content: {repr(exc)}"

    # Save document
    try:
        doc.save(target_path)
    except Exception as exc:
        return f"Failed to save document: {repr(exc)}"

    result = DeleteContentTextResponse(
        filepath=file_path,
        status="success",
        target=TargetInfo(kind=target_kind, identifier=identifier),
        result=summary,
    )

    return str(result)
