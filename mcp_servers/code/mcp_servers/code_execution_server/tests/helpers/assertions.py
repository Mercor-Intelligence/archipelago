"""Custom assertion helpers for code execution server tests."""

from models.code_exec import CodeExecResponse


def assert_success(result: CodeExecResponse, expected_substring: str | None = None):
    """Assert that command executed successfully."""
    assert result.success, f"Expected success but got error: {result.error}"

    if expected_substring is not None:
        output = result.output or ""
        assert expected_substring in output, (
            f"Expected substring '{expected_substring}' not found in output: {output}"
        )


def assert_error(result: CodeExecResponse, error_type: str | None = None):
    """Assert that command resulted in an error."""
    assert not result.success, f"Expected error but got success: {result.output}"

    if error_type is not None:
        error_msg = (result.error or "") + (result.output or "")
        assert error_type.lower() in error_msg.lower(), (
            f"Expected error type '{error_type}' not found in: {error_msg}"
        )


def assert_timeout(result: CodeExecResponse):
    """Assert that command timed out."""
    error_msg = (result.error or "") + (result.output or "")
    assert "timed out" in error_msg.lower(), (
        f"Expected timeout error but got: {error_msg}"
    )


def assert_exit_code(result: CodeExecResponse, exit_code: int):
    """Assert that command failed with specific exit code."""
    output = result.output or ""
    expected = f"exit code {exit_code}"
    assert expected in output.lower(), (
        f"Expected '{expected}' in output but got: {output}"
    )


def assert_contains_stderr(result: CodeExecResponse):
    """Assert that result contains stderr output."""
    output = result.output or ""
    assert "error output:" in output.lower(), (
        f"Expected stderr section in output but got: {output}"
    )


def assert_output_lines(result: CodeExecResponse, expected_count: int):
    """Assert that result has expected number of non-empty lines."""
    output = result.output or ""
    lines = [line for line in output.strip().split("\n") if line]
    actual_count = len(lines)
    assert actual_count == expected_count, (
        f"Expected {expected_count} lines but found {actual_count}. Output:\n{output}"
    )


def assert_empty_output(result: CodeExecResponse):
    """Assert that output is empty or whitespace only."""
    output = result.output or ""
    assert not output.strip(), f"Expected empty output but got: {output}"


def assert_file_created(workspace_path: str, filename: str):
    """Assert that a file was created in the workspace."""
    from pathlib import Path

    file_path = Path(workspace_path) / filename
    assert file_path.exists(), (
        f"Expected file '{filename}' to exist in workspace but it doesn't"
    )


def assert_file_content(workspace_path: str, filename: str, expected_content: str):
    """Assert that a file has expected content."""
    from pathlib import Path

    file_path = Path(workspace_path) / filename
    assert file_path.exists(), f"File '{filename}' does not exist in workspace"

    actual_content = file_path.read_text()
    assert expected_content in actual_content, (
        f"Expected content '{expected_content}' not found in file. "
        f"Actual content: {actual_content}"
    )


def assert_python_output(result: CodeExecResponse, expected: str):
    """Assert that Python code output matches expected value."""
    output = (result.output or "").strip()
    expected = expected.strip()
    assert output == expected, (
        f"Expected Python output:\n{expected}\n\nBut got:\n{output}"
    )
