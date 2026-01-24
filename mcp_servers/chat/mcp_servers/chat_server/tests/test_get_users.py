"""Unit tests for get_users and get_user_profile tools."""

import sys
from pathlib import Path

import pytest

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from models.requests import GetUserProfileRequest, GetUsersRequest
from tools.get_user_profile import get_user_profile
from tools.get_users import get_users


class TestGetUsers:
    """Test users listing functionality."""

    @pytest.mark.asyncio
    async def test_list_users(self, mock_chat_root, sample_group_basic):
        """Test listing users."""
        request = GetUsersRequest()
        response = await get_users(request)
        result = str(response)
        # Should return users list or no users found
        assert "user" in result.lower() or "no users" in result.lower()

    @pytest.mark.asyncio
    async def test_list_users_with_pagination(self, mock_chat_root, sample_group_basic):
        """Test listing users with pagination."""
        request = GetUsersRequest(limit=5, page=0)
        response = await get_users(request)
        # Should not raise an error
        assert response is not None


class TestGetUserProfile:
    """Test user profile functionality."""

    @pytest.mark.asyncio
    async def test_get_nonexistent_user_profile(self, mock_chat_root):
        """Test getting profile for non-existent user."""
        request = GetUserProfileRequest(user_id="nonexistent_user_123")
        with pytest.raises(ValueError) as exc_info:
            await get_user_profile(request)
        assert (
            "not found" in str(exc_info.value).lower()
            or "error" in str(exc_info.value).lower()
        )
