"""Unit tests for reply_to_thread tool."""

import sys
from pathlib import Path

import pytest

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from models.requests import PostMessageRequest, ReplyToThreadRequest
from tools.post_message import post_message
from tools.reply_to_thread import reply_to_thread

from .helpers.assertions import (
    assert_message_posted,
    assert_reply_posted,
)


class TestReplyToThread:
    """Test reply functionality."""

    @pytest.mark.asyncio
    async def test_reply_to_message(
        self, mock_chat_root, sample_group_basic, mock_current_user
    ):
        """Test replying to a message."""
        # Post original message
        post_request = PostMessageRequest(
            channel_id=sample_group_basic, message="Original message"
        )
        post_response = await post_message(post_request)
        post_result = str(post_response)
        message_id = assert_message_posted(post_result)

        # Reply to it
        reply_request = ReplyToThreadRequest(
            channel_id=sample_group_basic, post_id=message_id, message="This is a reply"
        )
        reply_response = await reply_to_thread(reply_request)
        result = str(reply_response)
        assert_reply_posted(result)
        assert "This is a reply" in result

    @pytest.mark.asyncio
    async def test_reply_to_nonexistent_message(
        self, mock_chat_root, sample_group_basic, mock_current_user
    ):
        """Test replying to non-existent message."""
        reply_request = ReplyToThreadRequest(
            channel_id=sample_group_basic, post_id="FAKE/ID/123", message="Reply text"
        )
        with pytest.raises(ValueError) as exc_info:
            await reply_to_thread(reply_request)
        assert "not found" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_reply_to_nonexistent_channel(
        self, mock_chat_root, mock_current_user
    ):
        """Test replying in non-existent channel."""
        reply_request = ReplyToThreadRequest(
            channel_id="Space NONEXISTENT", post_id="FAKE/ID/123", message="Reply"
        )
        with pytest.raises(ValueError) as exc_info:
            await reply_to_thread(reply_request)
        assert "not found" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_multiple_replies_to_same_message(
        self, mock_chat_root, sample_group_basic, mock_current_user
    ):
        """Test posting multiple replies to the same message."""
        # Post original message
        post_request = PostMessageRequest(
            channel_id=sample_group_basic, message="Original"
        )
        post_response = await post_message(post_request)
        post_result = str(post_response)
        message_id = assert_message_posted(post_result)

        # Post multiple replies
        reply_ids = []
        for i in range(3):
            reply_request = ReplyToThreadRequest(
                channel_id=sample_group_basic, post_id=message_id, message=f"Reply {i}"
            )
            reply_response = await reply_to_thread(reply_request)
            result = str(reply_response)
            assert_reply_posted(result)
            # Extract the actual reply ID, not the full result string
            reply_id = assert_message_posted(result)
            reply_ids.append(reply_id)

        # All reply IDs should be unique
        assert len(set(reply_ids)) == 3
