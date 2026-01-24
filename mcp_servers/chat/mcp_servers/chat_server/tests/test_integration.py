"""Integration tests for chat operations."""

import sys
from pathlib import Path

import pytest

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from models.requests import (
    AddReactionRequest,
    DeletePostRequest,
    GetChannelHistoryRequest,
    ListChannelsRequest,
    PostMessageRequest,
    ReplyToThreadRequest,
)
from tools.add_reaction import add_reaction
from tools.delete_post import delete_post
from tools.get_channel_history import get_channel_history
from tools.list_channels import list_channels
from tools.post_message import post_message
from tools.reply_to_thread import reply_to_thread

from .helpers.assertions import (
    assert_group_count,
    assert_message_count,
    assert_message_deleted,
    assert_message_in_history,
    assert_message_posted,
    assert_reaction_added,
    assert_reply_posted,
)


class TestIntegration:
    """Test integrated chat operations."""

    @pytest.mark.asyncio
    async def test_post_and_retrieve_workflow(
        self, mock_chat_root, sample_group_basic, mock_current_user
    ):
        """Test post → retrieve workflow."""
        # Post message
        post_request = PostMessageRequest(
            channel_id=sample_group_basic, message="Test message"
        )
        post_response = await post_message(post_request)
        post_result = str(post_response)
        _ = assert_message_posted(post_result)

        # Retrieve history
        history_request = GetChannelHistoryRequest(channel_id=sample_group_basic)
        history_response = await get_channel_history(history_request)
        history = str(history_response)
        assert_message_in_history(history, "Test message")
        assert_message_count(history, 1)

        # List channels
        list_request = ListChannelsRequest()
        list_response = await list_channels(list_request)
        channels = str(list_response)
        assert_group_count(channels, 1)

    @pytest.mark.asyncio
    async def test_conversation_thread_workflow(
        self, mock_chat_root, sample_group_basic, mock_current_user
    ):
        """Test complete conversation: post → reply → react."""
        # Post original message
        post_request = PostMessageRequest(
            channel_id=sample_group_basic, message="Original post"
        )
        post_response = await post_message(post_request)
        post_result = str(post_response)
        message_id = assert_message_posted(post_result)

        # Reply to it
        reply_request = ReplyToThreadRequest(
            channel_id=sample_group_basic, post_id=message_id, message="First reply"
        )
        reply_response = await reply_to_thread(reply_request)
        reply_result = str(reply_response)
        assert_reply_posted(reply_result)

        # Add reaction
        react_request = AddReactionRequest(
            channel_id=sample_group_basic, post_id=message_id, emoji_name="👍"
        )
        react_response = await add_reaction(react_request)
        reaction_result = str(react_response)
        assert_reaction_added(reaction_result, "👍")

        # Check history
        history_request = GetChannelHistoryRequest(channel_id=sample_group_basic)
        history_response = await get_channel_history(history_request)
        history = str(history_response)
        assert_message_count(history, 1)  # Only root messages

    @pytest.mark.asyncio
    async def test_delete_workflow(
        self, mock_chat_root, sample_group_basic, mock_current_user
    ):
        """Test post → delete workflow."""
        # Post message
        post_request = PostMessageRequest(
            channel_id=sample_group_basic, message="Delete me"
        )
        post_response = await post_message(post_request)
        post_result = str(post_response)
        message_id = assert_message_posted(post_result)

        # Verify it exists in history
        history_request = GetChannelHistoryRequest(channel_id=sample_group_basic)
        history_response = await get_channel_history(history_request)
        history = str(history_response)
        assert_message_count(history, 1)

        # Delete it
        delete_request = DeletePostRequest(
            channel_id=sample_group_basic, post_id=message_id
        )
        delete_response = await delete_post(delete_request)
        delete_result = str(delete_response)
        assert_message_deleted(delete_result, message_id)

        # History should still show the message (soft delete)
        history_after_response = await get_channel_history(history_request)
        history_after = str(history_after_response)
        assert_message_count(history_after, 1)

    @pytest.mark.asyncio
    async def test_multiple_channels_workflow(
        self, mock_chat_root, sample_multiple_groups, mock_current_user
    ):
        """Test operations across multiple channels."""
        # List all channels
        list_request = ListChannelsRequest()
        list_response = await list_channels(list_request)
        channels = str(list_response)
        assert_group_count(channels, 5)

        # Post to first channel
        first_group = sample_multiple_groups[0]
        post_request = PostMessageRequest(
            channel_id=first_group, message="Message in first group"
        )
        post_response = await post_message(post_request)
        result = str(post_response)
        message_id = assert_message_posted(result)
        assert message_id

        # Verify history only in that channel
        history_request = GetChannelHistoryRequest(channel_id=first_group)
        history_response = await get_channel_history(history_request)
        history = str(history_response)
        assert_message_count(history, 1)
