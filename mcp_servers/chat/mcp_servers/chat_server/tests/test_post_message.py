"""Unit tests for post_message tool."""

import sys
from pathlib import Path

import pytest

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from models.requests import PostMessageRequest
from tools.post_message import post_message

from .helpers.assertions import (
    assert_message_posted,
)


class TestPostMessage:
    """Test message posting functionality."""

    @pytest.mark.asyncio
    async def test_post_basic_message(
        self, mock_chat_root, sample_group_basic, mock_current_user
    ):
        """Test posting a basic message."""
        request = PostMessageRequest(
            channel_id=sample_group_basic, message="Hello, world!"
        )
        response = await post_message(request)
        result = str(response)
        message_id = assert_message_posted(result)
        assert message_id
        assert "Hello, world!" in result

    @pytest.mark.asyncio
    async def test_post_message_to_nonexistent_group(
        self, mock_chat_root, mock_current_user
    ):
        """Test posting to non-existent group."""
        request = PostMessageRequest(
            channel_id="Space NONEXISTENT", message="Test message"
        )
        with pytest.raises(ValueError) as exc_info:
            await post_message(request)
        assert "not found" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_post_empty_message(
        self, mock_chat_root, sample_group_basic, mock_current_user
    ):
        """Test posting empty message."""
        request = PostMessageRequest(channel_id=sample_group_basic, message="")
        response = await post_message(request)
        result = str(response)
        # Empty messages should be allowed
        message_id = assert_message_posted(result)
        assert message_id

    @pytest.mark.asyncio
    async def test_post_long_message(
        self, mock_chat_root, sample_group_basic, mock_current_user
    ):
        """Test posting a very long message."""
        long_message = "x" * 10000
        request = PostMessageRequest(
            channel_id=sample_group_basic, message=long_message
        )
        response = await post_message(request)
        result = str(response)
        message_id = assert_message_posted(result)
        assert message_id

    @pytest.mark.asyncio
    async def test_post_message_with_special_characters(
        self, mock_chat_root, sample_group_basic, mock_current_user
    ):
        """Test posting message with special characters."""
        special_msg = "Hello! @user #tag *bold* _italic_ `code` 😀"
        request = PostMessageRequest(channel_id=sample_group_basic, message=special_msg)
        response = await post_message(request)
        result = str(response)
        message_id = assert_message_posted(result)
        assert message_id

    @pytest.mark.asyncio
    async def test_post_message_with_newlines(
        self, mock_chat_root, sample_group_basic, mock_current_user
    ):
        """Test posting message with newlines."""
        multiline_msg = "Line 1\nLine 2\nLine 3"
        request = PostMessageRequest(
            channel_id=sample_group_basic, message=multiline_msg
        )
        response = await post_message(request)
        result = str(response)
        message_id = assert_message_posted(result)
        assert message_id

    @pytest.mark.asyncio
    async def test_post_multiple_messages(
        self, mock_chat_root, sample_group_basic, mock_current_user
    ):
        """Test posting multiple messages to same group."""
        message_ids = []

        for i in range(3):
            request = PostMessageRequest(
                channel_id=sample_group_basic, message=f"Message {i}"
            )
            response = await post_message(request)
            result = str(response)
            message_id = assert_message_posted(result)
            message_ids.append(message_id)

        # All message IDs should be unique
        assert len(message_ids) == len(set(message_ids))

    @pytest.mark.asyncio
    async def test_post_message_creates_message_id(
        self, mock_chat_root, sample_group_basic, mock_current_user
    ):
        """Test that posting a message creates a message ID."""
        request = PostMessageRequest(
            channel_id=sample_group_basic, message="Test message"
        )
        response = await post_message(request)
        result = str(response)
        message_id = assert_message_posted(result)
        # Message ID format: GROUP_CODE/TOPIC_ID/UNIQUE_ID
        assert "/" in message_id

    @pytest.mark.asyncio
    async def test_post_message_includes_timestamp(
        self, mock_chat_root, sample_group_basic, mock_current_user
    ):
        """Test that posted message includes timestamp."""
        request = PostMessageRequest(
            channel_id=sample_group_basic, message="Test message"
        )
        response = await post_message(request)
        result = str(response)
        assert "posted at:" in result.lower()

    @pytest.mark.asyncio
    async def test_post_message_to_group_with_existing_messages(
        self, mock_chat_root, sample_group_with_messages, mock_current_user
    ):
        """Test posting to a group that already has messages."""
        request = PostMessageRequest(
            channel_id=sample_group_with_messages, message="New message"
        )
        response = await post_message(request)
        result = str(response)
        message_id = assert_message_posted(result)
        assert message_id
        assert "New message" in result
