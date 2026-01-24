"""Custom assertion helpers for tests."""


def assert_success(result: str):
    """Assert that result indicates success (not an error).

    Args:
        result: The result string to check
    """
    assert not result.startswith("["), f"Expected success but got error: {result}"


def assert_error(result: str, error_type: str):
    """Assert that result contains expected error message.

    Args:
        result: The result string to check
        error_type: The expected error type (e.g., "not found", "permission denied")
    """
    assert f"[{error_type.lower()}:" in result.lower(), (
        f"Expected '{error_type}' error but got: {result}"
    )


def assert_file_listed(result: str, filename: str, mime_type: str | None = None):
    """Assert that a file is listed in the result.

    Args:
        result: The result string to check
        filename: The filename to look for
        mime_type: Optional MIME type to verify
    """
    lines = result.strip().split("\n")
    matching_lines = [line for line in lines if f"'{filename}'" in line]

    assert matching_lines, f"File '{filename}' not found in: {result}"

    if mime_type:
        file_line = matching_lines[0]
        assert mime_type in file_line, (
            f"MIME type '{mime_type}' not found on same line as {filename}. "
            f"Line: {file_line}"
        )


def assert_folder_listed(result: str, folder_name: str):
    """Assert that a folder is listed in the result.

    Args:
        result: The result string to check
        folder_name: The folder name to look for
    """
    assert f"'{folder_name}' (folder)" in result, (
        f"Folder '{folder_name}' not found in: {result}"
    )


def assert_file_count(result: str, expected_count: int):
    """Assert the number of items listed.

    Args:
        result: The result string to check
        expected_count: Expected number of items (files + folders)
    """
    if expected_count == 0:
        assert result == "No items found", f"Expected empty but got: {result}"
    else:
        # Count lines (each item is one line)
        lines = [line for line in result.strip().split("\n") if line]
        actual_count = len(lines)
        assert actual_count == expected_count, (
            f"Expected {expected_count} items but found {actual_count}"
        )


def assert_file_size(result: str, filename: str, expected_size: int):
    """Assert that a file has the expected size in bytes.

    Args:
        result: The result string to check
        filename: The filename to check
        expected_size: Expected size in bytes
    """
    lines = result.strip().split("\n")
    matching_lines = [line for line in lines if f"'{filename}'" in line]

    assert matching_lines, f"File '{filename}' not found"

    file_line = matching_lines[0]
    assert f"{expected_size} bytes" in file_line, (
        f"Expected {expected_size} bytes for {filename} on same line. Line: {file_line}"
    )
