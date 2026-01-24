"""Custom assertion helpers for chat server tests."""


def assert_success(result: str, expected_substring: str | None = None):
    """Assert that chat operation succeeded.

    Args:
        result: The result string from chat operation
        expected_substring: Optional substring to check for in result
    """
    result_lower = result.lower()
    # Check for error indicators
    assert "error:" not in result_lower, f"Expected success but got error: {result}"

    if expected_substring is not None:
        assert expected_substring.lower() in result_lower, (
            f"Expected '{expected_substring}' in result but got: {result}"
        )


def assert_error(result: str, error_substring: str | None = None):
    """Assert that chat operation failed.

    Args:
        result: The result string from chat operation
        error_substring: Optional error substring to check for
    """
    result_lower = result.lower()
    assert "error:" in result_lower, f"Expected error but got: {result}"

    if error_substring is not None:
        assert error_substring.lower() in result_lower, (
            f"Expected error '{error_substring}' in result but got: {result}"
        )


def assert_message_posted(result: str) -> str:
    """Assert message was posted successfully and return message ID.

    Args:
        result: The result string from post_message

    Returns:
        The posted message_id
    """
    assert_success(result)
    result_lower = result.lower()
    assert "message id" in result_lower or "message_id" in result_lower, (
        f"No message ID found in: {result}"
    )

    # Extract message ID (format: GROUP_CODE/TOPIC_ID/UNIQUE_ID)
    import re

    match = re.search(r"Message ID: ([A-Za-z0-9/]+)", result)
    assert match, f"Could not extract message ID from: {result}"

    message_id = match.group(1)
    assert message_id, f"Empty message ID extracted from: {result}"

    return message_id


def assert_group_exists(result: str, group_name: str):
    """Assert that a group exists in the result.

    Args:
        result: The result string from list_channels
        group_name: Name of the group to check for
    """
    assert group_name.lower() in result.lower(), (
        f"Group '{group_name}' not found in: {result}"
    )


def assert_group_count(result: str, expected_count: int):
    """Assert the number of groups in list result.

    Args:
        result: The result string from list_channels
        expected_count: Expected number of groups
    """
    result_lower = result.lower()
    if expected_count == 0:
        assert "no groups" in result_lower, f"Expected no groups but got: {result}"
    else:
        # Count group entries by looking for "ID:" occurrences (in the group list format)
        import re

        group_ids = re.findall(r"ID:\s*Space\s+[A-Za-z0-9]+", result, re.IGNORECASE)
        actual_count = len(group_ids)
        assert actual_count == expected_count, (
            f"Expected {expected_count} groups but found {actual_count}. Result:\n{result}"
        )


def assert_message_in_history(result: str, message_text: str):
    """Assert message appears in channel history.

    Args:
        result: The result string from get_channel_history
        message_text: Message text to check for
    """
    assert message_text in result, (
        f"Message text '{message_text}' not found in history: {result}"
    )


def assert_message_count(result: str, expected_count: int):
    """Assert the number of messages in history result.

    Args:
        result: The result string from get_channel_history
        expected_count: Expected number of messages
    """
    result_lower = result.lower()
    if expected_count == 0:
        assert "no messages" in result_lower, f"Expected no messages but got: {result}"
    else:
        # Count message entries by looking for "Message ID:" occurrences
        import re

        message_ids = re.findall(r"message id:", result, re.IGNORECASE)
        actual_count = len(message_ids)
        assert actual_count == expected_count, (
            f"Expected {expected_count} messages but found {actual_count}. Result:\n{result}"
        )


def assert_reply_posted(result: str):
    """Assert reply was posted successfully.

    Args:
        result: The result string from reply_to_thread
    """
    assert_success(result)
    assert "reply posted" in result.lower(), (
        f"Expected reply indicator but got: {result}"
    )


def assert_reaction_added(result: str, emoji: str):
    """Assert reaction was added successfully.

    Args:
        result: The result string from add_reaction
        emoji: The emoji that was added
    """
    assert_success(result, emoji)


def assert_message_deleted(result: str, post_id: str):
    """Assert message was deleted successfully.

    Args:
        result: The result string from delete_post
        post_id: ID of the deleted post
    """
    assert_success(result)
    assert post_id in result, f"Post ID '{post_id}' not found in result: {result}"


def assert_pagination_info(result: str, page: int, has_next: bool = False):
    """Assert pagination information in result.

    Args:
        result: The result string
        page: Expected page number
        has_next: Whether there should be a next page
    """
    assert f"page {page + 1}" in result.lower(), (
        f"Expected page {page + 1} in: {result}"
    )

    if has_next:
        assert "next page" in result.lower(), (
            f"Expected 'next page' indicator but got: {result}"
        )


def assert_user_in_list(result: str, email: str):
    """Assert user appears in users list.

    Args:
        result: The result string from get_users
        email: Email to check for
    """
    assert email in result, f"User email '{email}' not found in: {result}"


def assert_thread_has_replies(result: str, min_count: int = 1):
    """Assert thread has replies.

    Args:
        result: The result string from get_thread_replies
        min_count: Minimum number of replies expected
    """
    assert_success(result)
    # Check that there are replies
    import re

    message_ids = re.findall(r"message id:", result, re.IGNORECASE)
    actual_count = len(message_ids)
    assert actual_count >= min_count, (
        f"Expected at least {min_count} replies but found {actual_count}. Result:\n{result}"
    )
