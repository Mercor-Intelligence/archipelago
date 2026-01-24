"""Unit tests for add_reaction tool."""

import sys
from pathlib import Path

import pytest

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from models.requests import AddReactionRequest, PostMessageRequest
from tools.add_reaction import add_reaction
from tools.post_message import post_message

from .helpers.assertions import (
    assert_message_posted,
    assert_reaction_added,
)


class TestAddReaction:
    """Test reaction functionality."""

    @pytest.mark.asyncio
    async def test_add_reaction_to_message(
        self, mock_chat_root, sample_group_basic, mock_current_user
    ):
        """Test adding a reaction to a message."""
        # Post message
        post_request = PostMessageRequest(
            channel_id=sample_group_basic, message="React to this!"
        )
        post_response = await post_message(post_request)
        post_result = str(post_response)
        message_id = assert_message_posted(post_result)

        # Add reaction
        react_request = AddReactionRequest(
            channel_id=sample_group_basic, post_id=message_id, emoji_name="👍"
        )
        react_response = await add_reaction(react_request)
        result = str(react_response)
        assert_reaction_added(result, "👍")

    @pytest.mark.asyncio
    async def test_add_reaction_to_nonexistent_message(
        self, mock_chat_root, sample_group_basic, mock_current_user
    ):
        """Test adding reaction to non-existent message."""
        react_request = AddReactionRequest(
            channel_id=sample_group_basic, post_id="FAKE/ID/123", emoji_name="👍"
        )
        with pytest.raises(ValueError) as exc_info:
            await add_reaction(react_request)
        assert "not found" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_add_duplicate_reaction(
        self, mock_chat_root, sample_group_basic, mock_current_user
    ):
        """Test adding the same reaction twice."""
        # Post message
        post_request = PostMessageRequest(
            channel_id=sample_group_basic, message="Message"
        )
        post_response = await post_message(post_request)
        post_result = str(post_response)
        message_id = assert_message_posted(post_result)

        # Add reaction
        react_request = AddReactionRequest(
            channel_id=sample_group_basic, post_id=message_id, emoji_name="👍"
        )
        react_response = await add_reaction(react_request)
        result1 = str(react_response)
        assert_reaction_added(result1, "👍")

        # Try to add same reaction again - should raise or return already exists
        with pytest.raises(ValueError) as exc_info:
            await add_reaction(react_request)
        assert "already" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_add_multiple_different_reactions(
        self, mock_chat_root, sample_group_basic, mock_current_user
    ):
        """Test adding different reactions to the same message."""
        # Post message
        post_request = PostMessageRequest(
            channel_id=sample_group_basic, message="Message"
        )
        post_response = await post_message(post_request)
        post_result = str(post_response)
        message_id = assert_message_posted(post_result)

        # Add different reactions
        emojis = ["👍", "❤️", "😀"]
        for emoji in emojis:
            react_request = AddReactionRequest(
                channel_id=sample_group_basic, post_id=message_id, emoji_name=emoji
            )
            react_response = await add_reaction(react_request)
            result = str(react_response)
            assert_reaction_added(result, emoji)
