"""Custom assertion helpers for mail server tests."""


def assert_success(result: str, expected_substring: str | None = None):
    """Assert that mail operation succeeded.

    Args:
        result: The result string from mail operation
        expected_substring: Optional substring to check for in result
    """
    result_lower = result.lower()
    assert (
        "success: true" in result_lower or "mail sent successfully" in result_lower
    ), f"Expected success but got: {result}"

    if expected_substring is not None:
        assert expected_substring.lower() in result_lower, (
            f"Expected '{expected_substring}' in result but got: {result}"
        )


def assert_failure(result: str, error_substring: str | None = None):
    """Assert that mail operation failed.

    Args:
        result: The result string from mail operation
        error_substring: Optional error substring to check for
    """
    result_lower = result.lower()
    assert "success: false" in result_lower or "failed" in result_lower, (
        f"Expected failure but got: {result}"
    )

    if error_substring is not None:
        assert error_substring.lower() in result_lower, (
            f"Expected error '{error_substring}' in result but got: {result}"
        )


def assert_mail_id_in_result(result: str) -> str:
    """Extract and validate mail ID from result.

    Args:
        result: The result string from mail operation

    Returns:
        The extracted mail_id
    """
    result_lower = result.lower()
    assert "mail id" in result_lower or "mail_id" in result_lower, (
        f"No mail ID found in: {result}"
    )

    # Extract mail ID (format: <timestamp.unique@domain>)
    import re

    match = re.search(r"<[^>]+>", result)
    assert match, f"Could not extract mail ID from: {result}"

    mail_id = match.group(0)
    assert mail_id, f"Empty mail ID extracted from: {result}"

    return mail_id


def assert_mail_sent(result: str) -> str:
    """Assert mail was sent successfully and return mail ID.

    Args:
        result: The result string from send_mail

    Returns:
        The sent mail_id
    """
    assert_success(result, "mail sent successfully")
    return assert_mail_id_in_result(result)


def assert_validation_error(result: str, field: str | None = None):
    """Assert that a validation error occurred.

    Args:
        result: The result string from mail operation
        field: Optional field name that should be in the error
    """
    result_lower = result.lower()
    error_keywords = ["validation failed", "invalid", "error"]
    has_error = any(keyword in result_lower for keyword in error_keywords)
    assert has_error, f"Expected validation/error message but got: {result}"

    if field is not None:
        assert field.lower() in result_lower, (
            f"Expected field '{field}' in validation error but got: {result}"
        )


def assert_mail_not_found(result: str):
    """Assert that mail was not found.

    Args:
        result: The result string from mail operation
    """
    result_lower = result.lower()
    assert (
        "not found" in result_lower
        or "no mail found" in result_lower
        or "failed" in result_lower
    ), f"Expected 'not found' error but got: {result}"


def assert_mail_count(result: str, expected_count: int):
    """Assert the number of mails in list result.

    Args:
        result: The result string from list_mails
        expected_count: Expected number of mails
    """
    if expected_count == 0:
        assert "no emails found" in result.lower() or "mails: []" in result.lower(), (
            f"Expected no mails but got: {result}"
        )
    else:
        # Count mail entries by looking for "Mail ID:" occurrences
        import re

        # Match lines that contain "Mail ID:" to count actual mail entries
        mail_id_lines = re.findall(r"mail id:", result, re.IGNORECASE)
        actual_count = len(mail_id_lines)
        assert actual_count == expected_count, (
            f"Expected {expected_count} mails but found {actual_count}. Result:\n{result}"
        )


def assert_mail_in_list(result: str, mail_id: str):
    """Assert mail ID appears in list result.

    Args:
        result: The result string from list_mails
        mail_id: Mail ID to check for
    """
    assert mail_id in result, f"Mail ID '{mail_id}' not found in list: {result}"


def assert_mail_field(result: str, field_name: str, expected_value: str):
    """Assert mail contains expected field value.

    Args:
        result: The result string from read_mail
        field_name: Name of the field to check
        expected_value: Expected value for the field
    """
    result_lower = result.lower()
    field_lower = field_name.lower()

    # Check field exists
    assert field_lower in result_lower, f"Field '{field_name}' not found in: {result}"

    # Check value exists
    expected_lower = expected_value.lower()
    assert expected_lower in result_lower, (
        f"Expected value '{expected_value}' not found in field '{field_name}'. "
        f"Result: {result}"
    )


def assert_recipients_count(result: str, expected_count: int):
    """Assert the number of recipients in result.

    Args:
        result: The result string from send_mail
        expected_count: Expected number of recipients
    """
    assert f"recipients: {expected_count}" in result.lower(), (
        f"Expected {expected_count} recipients but got: {result}"
    )


def assert_subject_prefix(result: str, prefix: str):
    """Assert subject has expected prefix (Re: or Fwd:).

    Args:
        result: The result string from read_mail
        prefix: Expected prefix (e.g., "Re:", "Fwd:")
    """
    result_lower = result.lower()
    assert (
        f"subject: {prefix.lower()}" in result_lower
        or f"subject:{prefix.lower()}" in result_lower
    ), f"Expected subject with prefix '{prefix}' but got: {result}"


def assert_thread_preserved(result: str, thread_id: str):
    """Assert thread_id is preserved in result.

    Args:
        result: The result string from read_mail
        thread_id: Expected thread_id
    """
    assert thread_id in result, f"Thread ID '{thread_id}' not found in: {result}"


def assert_search_results(result: str, min_count: int = 1):
    """Assert search returned results.

    Args:
        result: The result string from search_mail
        min_count: Minimum number of results expected
    """
    import re

    mail_ids = re.findall(r"<[^>]+>", result)
    actual_count = len(mail_ids)
    assert actual_count >= min_count, (
        f"Expected at least {min_count} search results but found {actual_count}. "
        f"Result:\n{result}"
    )
