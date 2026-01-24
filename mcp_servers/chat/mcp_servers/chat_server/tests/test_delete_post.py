"""Unit tests for delete_post tool."""

import sys
from pathlib import Path

import pytest

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from models.requests import DeletePostRequest, PostMessageRequest
from tools.delete_post import delete_post
from tools.post_message import post_message

from .helpers.assertions import (
    assert_message_deleted,
    assert_message_posted,
)


class TestDeletePost:
    """Test message deletion functionality."""

    @pytest.mark.asyncio
    async def test_delete_message(
        self, mock_chat_root, sample_group_basic, mock_current_user
    ):
        """Test deleting a message."""
        # Post message
        post_request = PostMessageRequest(
            channel_id=sample_group_basic, message="Delete me"
        )
        post_response = await post_message(post_request)
        post_result = str(post_response)
        message_id = assert_message_posted(post_result)

        # Delete it
        delete_request = DeletePostRequest(
            channel_id=sample_group_basic, post_id=message_id
        )
        delete_response = await delete_post(delete_request)
        result = str(delete_response)
        assert_message_deleted(result, message_id)

    @pytest.mark.asyncio
    async def test_delete_nonexistent_message(self, mock_chat_root, sample_group_basic):
        """Test deleting non-existent message."""
        delete_request = DeletePostRequest(
            channel_id=sample_group_basic, post_id="FAKE/ID/123"
        )
        with pytest.raises(ValueError) as exc_info:
            await delete_post(delete_request)
        assert "not found" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_delete_already_deleted_message(
        self, mock_chat_root, sample_group_basic, mock_current_user
    ):
        """Test deleting an already deleted message."""
        # Post message
        post_request = PostMessageRequest(
            channel_id=sample_group_basic, message="Delete me"
        )
        post_response = await post_message(post_request)
        post_result = str(post_response)
        message_id = assert_message_posted(post_result)

        # Delete it
        delete_request = DeletePostRequest(
            channel_id=sample_group_basic, post_id=message_id
        )
        delete_response = await delete_post(delete_request)
        result1 = str(delete_response)
        assert_message_deleted(result1, message_id)

        # Try to delete again
        with pytest.raises(ValueError) as exc_info:
            await delete_post(delete_request)
        assert "already deleted" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_delete_message_in_nonexistent_channel(self, mock_chat_root):
        """Test deleting message in non-existent channel."""
        delete_request = DeletePostRequest(
            channel_id="Space NONEXISTENT", post_id="FAKE/ID/123"
        )
        with pytest.raises(ValueError) as exc_info:
            await delete_post(delete_request)
        assert "not found" in str(exc_info.value).lower()
