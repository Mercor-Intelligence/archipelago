"""Custom assertion helpers for docs server tests."""


def assert_success(result: str, expected_substring: str | None = None):
    """Assert that docs operation succeeded.

    Args:
        result: The result string from docs operation
        expected_substring: Optional substring to check for in result
    """
    # Check the first 200 characters for error patterns
    # This avoids false positives from document content that mentions errors
    result_start = result[:200].lower()

    # Success indicators - check for operation success keywords
    has_success = (
        "status: success" in result_start
        or "created" in result_start
        or "deleted" in result_start
        or "added" in result_start
        or "formatting" in result_start
        or "document content:" in result_start
        or "document overview:" in result_start
        or "rotated" in result_start
        or "flipped" in result_start
        or "adjusted" in result_start
        or "resized" in result_start
        or "brightness" in result_start
        or "contrast" in result_start
        or "image" in result_start
    )

    # Error indicators (only check beginning)
    has_error = (
        "error:" in result_start
        or result_start.startswith("file not found")
        or result_start.startswith("image file not found")
        or result_start.startswith("file path must")
        or result_start.startswith("directory must")
        or result_start.startswith("failed")
        or result_start.startswith("no images found")
        or result_start.startswith("no headings found")
    )

    # Must have success indicators AND not have error indicators
    assert has_success, f"Expected success indicators but got: {result[:500]}"
    assert not has_error, f"Expected success but got error: {result[:500]}"

    if expected_substring is not None:
        assert expected_substring.lower() in result.lower(), (
            f"Expected '{expected_substring}' in result but got: {result[:500]}"
        )


def assert_error(result: str, error_substring: str | None = None):
    """Assert that docs operation failed.

    Args:
        result: The result string from docs operation
        error_substring: Optional error substring to check for
    """
    result_lower = result.lower()
    # Check for common error indicators or validation messages
    error_indicators = [
        "error:",
        "failed",
        "not found",
        "not a file",
        "must",
        "cannot",
        "required",
        "invalid",
        "out of range",
        "no images",
        "unsupported",
    ]
    has_error = any(indicator in result_lower for indicator in error_indicators)

    assert has_error, f"Expected error but got: {result}"

    if error_substring is not None:
        assert error_substring.lower() in result_lower, (
            f"Expected error '{error_substring}' in result but got: {result}"
        )


def assert_document_created(result: str, file_name: str):
    """Assert document was created successfully.

    Args:
        result: The result string from create_document
        file_name: Expected file name
    """
    assert_success(result)
    assert file_name in result, f"File name '{file_name}' not found in: {result}"
    assert "created" in result.lower(), f"Expected 'created' in result: {result}"


def assert_document_read(result: str):
    """Assert document was read successfully.

    Args:
        result: The result string from read_document_content
    """
    assert_success(result)
    assert "filepath:" in result.lower() or "document" in result.lower(), (
        f"Expected document content in: {result}"
    )


def assert_identifier_in_result(result: str, identifier: str):
    """Assert identifier appears in result.

    Args:
        result: The result string
        identifier: Identifier to check for
    """
    assert identifier in result, f"Identifier '{identifier}' not found in: {result}"


def assert_text_in_document(result: str, text: str):
    """Assert text appears in document content.

    Args:
        result: The result string from read_document_content
        text: Text to check for
    """
    assert text in result, f"Text '{text}' not found in document: {result}"


def assert_heading_count(result: str, expected_count: int):
    """Assert the number of headings in overview.

    Args:
        result: The result string from get_document_overview
        expected_count: Expected number of headings
    """
    if expected_count == 0:
        assert "no headings" in result.lower(), (
            f"Expected no headings but got: {result}"
        )
    else:
        import re

        headings = re.findall(r"\[body\.p\.\d+\]", result)
        actual_count = len(headings)
        assert actual_count == expected_count, (
            f"Expected {expected_count} headings but found {actual_count}. Result:\n{result}"
        )


def assert_edit_applied(result: str, identifier: str, new_text: str):
    """Assert edit was applied successfully.

    Args:
        result: The result string from edit_content_text
        identifier: Identifier that was edited
        new_text: New text that was set
    """
    assert_success(result)
    assert identifier in result, f"Identifier '{identifier}' not found in: {result}"
    assert new_text in result, f"New text '{new_text}' not found in: {result}"


def assert_content_added(result: str):
    """Assert content was added successfully.

    Args:
        result: The result string from add_content_text
    """
    assert_success(result, "added")


def assert_content_deleted(result: str, identifier: str):
    """Assert content was deleted successfully.

    Args:
        result: The result string from delete_content_text
        identifier: Identifier that was deleted
    """
    assert_success(result, "deleted")
    assert identifier in result, f"Identifier '{identifier}' not found in: {result}"


def assert_formatting_applied(result: str):
    """Assert formatting was applied successfully.

    Args:
        result: The result string from apply_formatting
    """
    assert_success(result, "formatting")


def assert_document_deleted(result: str, file_path: str):
    """Assert document was deleted successfully.

    Args:
        result: The result string from delete_document
        file_path: Path of deleted document
    """
    assert_success(result, "deleted")
    assert file_path in result, f"File path '{file_path}' not found in: {result}"


def assert_image_annotation(result: str):
    """Assert result contains image annotation.

    Args:
        result: The result string from read_document_content
    """
    import re

    annotations = re.findall(r"@[a-z0-9_]+", result)
    assert len(annotations) > 0, f"No image annotations found in: {result}"


def assert_table_in_document(result: str):
    """Assert document contains table.

    Args:
        result: The result string from read_document_content
    """
    assert "tbl" in result.lower(), f"No table found in document: {result}"


def assert_paragraph_count(result: str, min_count: int = 1):
    """Assert minimum number of paragraphs in document.

    Args:
        result: The result string from read_document_content
        min_count: Minimum number of paragraphs expected
    """
    import re

    paragraphs = re.findall(r"body\.p\.\d+", result)
    actual_count = len(set(paragraphs))  # Unique paragraph IDs
    assert actual_count >= min_count, (
        f"Expected at least {min_count} paragraphs but found {actual_count}. Result:\n{result}"
    )
