"""Unit tests for list_channels tool."""

import sys
from pathlib import Path

import pytest

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from models.requests import ListChannelsRequest
from tools.list_channels import list_channels

from .helpers.assertions import (
    assert_group_count,
    assert_group_exists,
    assert_pagination_info,
)


class TestListChannels:
    """Test channel listing functionality."""

    @pytest.mark.asyncio
    async def test_list_empty_workspace(self, mock_chat_root):
        """Test listing channels when workspace is empty."""
        request = ListChannelsRequest()
        response = await list_channels(request)
        result = str(response)
        assert_group_count(result, 0)

    @pytest.mark.asyncio
    async def test_list_single_channel(self, mock_chat_root, sample_group_basic):
        """Test listing workspace with single channel."""
        request = ListChannelsRequest()
        response = await list_channels(request)
        result = str(response)
        assert_group_count(result, 1)
        assert_group_exists(result, "Test Group")

    @pytest.mark.asyncio
    async def test_list_multiple_channels(self, mock_chat_root, sample_multiple_groups):
        """Test listing multiple channels."""
        request = ListChannelsRequest()
        response = await list_channels(request)
        result = str(response)
        assert_group_count(result, 5)

    @pytest.mark.asyncio
    async def test_list_with_pagination(
        self, mock_chat_root, sample_multiple_groups, mock_pagination_limits
    ):
        """Test listing with pagination."""
        request = ListChannelsRequest(limit=2, page=0)
        response = await list_channels(request)
        result = str(response)
        assert_group_count(result, 2)
        assert_pagination_info(result, page=0)

    @pytest.mark.asyncio
    async def test_list_second_page(
        self, mock_chat_root, sample_multiple_groups, mock_pagination_limits
    ):
        """Test listing second page."""
        request = ListChannelsRequest(limit=2, page=1)
        response = await list_channels(request)
        result = str(response)
        assert_group_count(result, 2)
        assert_pagination_info(result, page=1)

    @pytest.mark.asyncio
    async def test_list_with_large_limit(self, mock_chat_root, sample_multiple_groups):
        """Test listing with large limit."""
        request = ListChannelsRequest(limit=100)
        response = await list_channels(request)
        result = str(response)
        assert_group_count(result, 5)
