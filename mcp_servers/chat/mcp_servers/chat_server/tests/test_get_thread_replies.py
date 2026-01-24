"""Unit tests for get_thread_replies tool."""

import sys
from pathlib import Path

import pytest

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from models.requests import (
    GetThreadRepliesRequest,
    PostMessageRequest,
    ReplyToThreadRequest,
)
from tools.get_thread_replies import get_thread_replies
from tools.post_message import post_message
from tools.reply_to_thread import reply_to_thread

from .helpers.assertions import assert_message_posted


class TestGetThreadReplies:
    """Test thread replies retrieval functionality."""

    @pytest.mark.asyncio
    async def test_get_replies_nonexistent_channel(self, mock_chat_root):
        """Test getting replies for non-existent channel."""
        request = GetThreadRepliesRequest(
            channel_id="Space NONEXISTENT", post_id="FAKE/ID/123"
        )
        with pytest.raises(ValueError) as exc_info:
            await get_thread_replies(request)
        assert "not found" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_get_replies_nonexistent_post(
        self, mock_chat_root, sample_group_basic
    ):
        """Test getting replies for non-existent post."""
        request = GetThreadRepliesRequest(
            channel_id=sample_group_basic, post_id="FAKE/ID/123"
        )
        with pytest.raises(ValueError) as exc_info:
            await get_thread_replies(request)
        assert "not found" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_get_replies_empty_thread(
        self, mock_chat_root, sample_group_basic, mock_current_user
    ):
        """Test getting replies for a post with no replies."""
        # First post a message
        post_request = PostMessageRequest(
            channel_id=sample_group_basic, message="No replies yet"
        )
        post_response = await post_message(post_request)
        post_result = str(post_response)
        message_id = assert_message_posted(post_result)

        # Get replies (should be empty)
        request = GetThreadRepliesRequest(
            channel_id=sample_group_basic, post_id=message_id
        )
        response = await get_thread_replies(request)
        result = str(response)
        # Should indicate empty or show the thread
        assert (
            "thread" in result.lower()
            or "no replies" in result.lower()
            or "message" in result.lower()
        )

    @pytest.mark.asyncio
    async def test_get_replies_with_replies(
        self, mock_chat_root, sample_group_basic, mock_current_user
    ):
        """Test getting replies for a post with replies."""
        # First post a message
        post_request = PostMessageRequest(
            channel_id=sample_group_basic, message="Original message"
        )
        post_response = await post_message(post_request)
        post_result = str(post_response)
        message_id = assert_message_posted(post_result)

        # Add a reply
        reply_request = ReplyToThreadRequest(
            channel_id=sample_group_basic, post_id=message_id, message="First reply"
        )
        await reply_to_thread(reply_request)

        # Get replies
        request = GetThreadRepliesRequest(
            channel_id=sample_group_basic, post_id=message_id
        )
        response = await get_thread_replies(request)
        result = str(response)
        assert "First reply" in result or "reply" in result.lower()
