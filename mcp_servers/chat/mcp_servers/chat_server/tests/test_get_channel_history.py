"""Unit tests for get_channel_history tool."""

import sys
from pathlib import Path

import pytest

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from models.requests import GetChannelHistoryRequest
from tools.get_channel_history import get_channel_history

from .helpers.assertions import (
    assert_message_count,
    assert_message_in_history,
)


class TestGetChannelHistory:
    """Test channel history retrieval functionality."""

    @pytest.mark.asyncio
    async def test_get_history_nonexistent_channel(self, mock_chat_root):
        """Test getting history for non-existent channel."""
        request = GetChannelHistoryRequest(channel_id="Space NONEXISTENT")
        with pytest.raises(ValueError) as exc_info:
            await get_channel_history(request)
        assert "not found" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_get_history_empty_channel(self, mock_chat_root, sample_group_basic):
        """Test getting history for empty channel."""
        request = GetChannelHistoryRequest(channel_id=sample_group_basic)
        response = await get_channel_history(request)
        result = str(response)
        assert_message_count(result, 0)

    @pytest.mark.asyncio
    async def test_get_history_with_messages(
        self, mock_chat_root, sample_group_with_messages
    ):
        """Test getting history for channel with messages."""
        request = GetChannelHistoryRequest(channel_id=sample_group_with_messages)
        response = await get_channel_history(request)
        result = str(response)
        assert_message_count(result, 1)
        assert_message_in_history(result, "Hello everyone!")

    @pytest.mark.asyncio
    async def test_get_history_with_pagination(
        self, mock_chat_root, sample_group_with_messages, mock_pagination_limits
    ):
        """Test getting history with pagination."""
        request = GetChannelHistoryRequest(
            channel_id=sample_group_with_messages, limit=1, page=0
        )
        response = await get_channel_history(request)
        result = str(response)
        assert_message_count(result, 1)
