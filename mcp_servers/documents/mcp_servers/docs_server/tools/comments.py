"""Comments tool for reading, adding, and deleting comments in Documents documents."""

import os

from docx import Document
from helpers.identifier import parse_identifier
from helpers.mutate import resolve_target
from utils.decorators import make_async_background
from utils.models import (
    CommentInfo,
    CommentsAddResponse,
    CommentsDeleteResponse,
    CommentsReadResponse,
)

DOCS_ROOT = os.getenv("APP_DOCS_ROOT") or os.getenv("APP_FS_ROOT", "/filesystem")


def _resolve_under_root(path: str) -> str:
    """Map path to the docs root."""
    path = path.lstrip("/")
    full_path = os.path.join(DOCS_ROOT, path)
    return os.path.normpath(full_path)


@make_async_background
def comments(
    file_path: str,
    action: str,
    identifier: str | None = None,
    text: str | None = None,
    author: str | None = None,
    comment_id: int | None = None,
) -> str:
    """Read, add, and delete comments in Documents documents.

    Args:
        file_path: Path to the .docx file (must start with /)
        action: Action to perform: "read", "add", or "delete"
        identifier: Target identifier for "add" action (e.g., 'body.p.0')
        text: Comment text for "add" action
        author: Comment author for "add" action (optional)
        comment_id: Comment ID for "delete" action

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
    valid_actions = {"read", "add", "delete"}
    if action not in valid_actions:
        return f"Invalid action: {action}. Must be one of: {', '.join(sorted(valid_actions))}"

    # Validate action-specific params
    if action == "add":
        if identifier is None:
            return "Identifier is required for 'add' action"
        if identifier == "":
            return "Identifier is required for 'add' action"
        if text is None:
            return "Text is required for 'add' action"
        if text == "":
            return "Text is required for 'add' action"
    if action == "delete":
        if comment_id is None:
            return "comment_id is required for 'delete' action"

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

    # Check if comments are supported
    if not hasattr(doc, "comments"):
        return "Comments not supported. Requires python-docx 1.2.0 or later."

    # Handle each action
    if action == "read":
        comment_list: list[CommentInfo] = []
        try:
            for comment in doc.comments:
                comment_list.append(
                    CommentInfo(
                        id=comment.comment_id,
                        author=comment.author or "",
                        text=comment.text or "",
                        date=str(comment.timestamp) if comment.timestamp else None,
                    )
                )
        except Exception as exc:
            return f"Failed to read comments: {repr(exc)}"

        result = CommentsReadResponse(
            filepath=file_path,
            status="success",
            comment_count=len(comment_list),
            comments=comment_list,
        )
        return str(result)

    elif action == "add":
        if identifier is None or identifier == "":
            return "Identifier is required for 'add' action"
        if text is None or text == "":
            return "Text is required for 'add' action"
        identifier_value = identifier
        text_value = text

        # Resolve identifier to get target runs
        try:
            parsed = parse_identifier(identifier_value)
            target_kind, target_obj, target_type = resolve_target(doc, parsed)
        except Exception as exc:
            return f"Failed to parse identifier '{identifier_value}': {repr(exc)}"

        # Get runs to attach comment to
        runs = []
        if target_type == "run":
            runs = [target_obj]
        elif target_type == "paragraph":
            runs = list(target_obj.runs)
            if not runs:
                # Create a run if paragraph has none
                target_obj.add_run("")
                runs = list(target_obj.runs)
        elif target_type == "cell":
            # Get runs from first paragraph in cell
            if target_obj.paragraphs:
                p = target_obj.paragraphs[0]
                runs = list(p.runs)
                if not runs:
                    p.add_run("")
                    runs = list(p.runs)
        else:
            return f"Cannot add comment to target type: {target_type}"

        if not runs:
            return "No runs found at target to attach comment"

        # Add the comment
        try:
            comment = doc.add_comment(runs=runs, text=text_value, author=author or "")
            new_comment_id = comment.comment_id
        except Exception as exc:
            return f"Failed to add comment: {repr(exc)}"

        # Save document
        try:
            doc.save(target_path)
        except Exception as exc:
            return f"Failed to save document: {repr(exc)}"

        result = CommentsAddResponse(
            filepath=file_path,
            status="success",
            comment_id=new_comment_id,
            identifier=identifier_value,
            text=text_value,
            author=author or "",
        )
        return str(result)

    elif action == "delete":
        if comment_id is None:
            return "comment_id is required for 'delete' action"
        comment_id_value = comment_id

        # Find and delete the comment
        try:
            comment_to_delete = None
            for comment in doc.comments:
                if comment.comment_id == comment_id_value:
                    comment_to_delete = comment
                    break

            if comment_to_delete is None:
                return f"Comment with id {comment_id_value} not found"

            # Store info before deletion
            deleted_author = comment_to_delete.author or ""
            deleted_text = comment_to_delete.text or ""

            # Delete the comment by removing its XML element
            comment_to_delete._element.getparent().remove(comment_to_delete._element)
        except Exception as exc:
            return f"Failed to delete comment: {repr(exc)}"

        # Save document
        try:
            doc.save(target_path)
        except Exception as exc:
            return f"Failed to save document: {repr(exc)}"

        result = CommentsDeleteResponse(
            filepath=file_path,
            status="success",
            comment_id=comment_id_value,
            deleted_author=deleted_author,
            deleted_text=deleted_text,
        )
        return str(result)

    else:
        return f"Unknown action: {action}"
