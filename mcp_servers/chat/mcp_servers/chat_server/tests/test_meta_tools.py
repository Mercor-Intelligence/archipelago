"""Unit tests for meta-tools."""

import sys
from pathlib import Path

import pytest

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from tools._meta_tools import (
    CHAT_HELP,
    ChatInput,
    SchemaInput,
    chat,
    chat_schema,
)


class TestChatMetaTool:
    """Test the consolidated chat meta-tool."""

    @pytest.mark.asyncio
    async def test_help_action(self):
        """Test help action returns available actions."""
        request = ChatInput(action="help")
        result = await chat(request)

        assert result.action == "help"
        assert result.error is None
        assert result.help is not None
        assert result.help.tool_name == "chat"
        assert "list_channels" in result.help.actions
        assert "post" in result.help.actions
        assert "react" in result.help.actions

    @pytest.mark.asyncio
    async def test_list_channels_action(self, mock_chat_root, sample_group_basic):
        """Test list_channels action."""
        request = ChatInput(action="list_channels")
        result = await chat(request)

        assert result.action == "list_channels"
        assert result.error is None
        assert result.list_channels is not None
        assert result.list_channels.total >= 0

    @pytest.mark.asyncio
    async def test_list_channels_with_pagination(
        self, mock_chat_root, sample_multiple_groups
    ):
        """Test list_channels with pagination."""
        request = ChatInput(action="list_channels", limit=2, page=0)
        result = await chat(request)

        assert result.action == "list_channels"
        assert result.error is None
        assert result.list_channels is not None
        assert len(result.list_channels.channels) <= 2

    @pytest.mark.asyncio
    async def test_post_action(
        self, mock_chat_root, sample_group_basic, mock_current_user
    ):
        """Test post action creates a message."""
        request = ChatInput(
            action="post",
            channel_id=sample_group_basic,
            message="Hello from meta-tool!",
        )
        result = await chat(request)

        assert result.action == "post"
        assert result.error is None
        assert result.post is not None
        assert result.post.message_id
        assert result.post.content == "Hello from meta-tool!"

    @pytest.mark.asyncio
    async def test_post_empty_message(
        self, mock_chat_root, sample_group_basic, mock_current_user
    ):
        """Test post action allows empty messages."""
        request = ChatInput(
            action="post",
            channel_id=sample_group_basic,
            message="",  # Empty message should be allowed
        )
        result = await chat(request)

        assert result.action == "post"
        assert result.error is None
        assert result.post is not None
        assert result.post.message_id

    @pytest.mark.asyncio
    async def test_post_missing_params(self):
        """Test post action with missing required params."""
        request = ChatInput(action="post")  # Missing channel_id and message
        result = await chat(request)

        assert result.action == "post"
        assert result.error is not None
        assert "channel_id" in result.error.lower() or "message" in result.error.lower()

    @pytest.mark.asyncio
    async def test_reply_action(
        self, mock_chat_root, sample_group_basic, mock_current_user
    ):
        """Test reply action creates a thread reply."""
        # First post a message
        post_request = ChatInput(
            action="post", channel_id=sample_group_basic, message="Original message"
        )
        post_result = await chat(post_request)
        message_id = post_result.post.message_id

        # Reply to it
        reply_request = ChatInput(
            action="reply",
            channel_id=sample_group_basic,
            post_id=message_id,
            message="This is a reply",
        )
        reply_result = await chat(reply_request)

        assert reply_result.action == "reply"
        assert reply_result.error is None
        assert reply_result.reply is not None
        assert reply_result.reply.content == "This is a reply"

    @pytest.mark.asyncio
    async def test_react_action(
        self, mock_chat_root, sample_group_basic, mock_current_user
    ):
        """Test react action adds a reaction."""
        # First post a message
        post_request = ChatInput(
            action="post", channel_id=sample_group_basic, message="React to this!"
        )
        post_result = await chat(post_request)
        message_id = post_result.post.message_id

        # React to it
        react_request = ChatInput(
            action="react",
            channel_id=sample_group_basic,
            post_id=message_id,
            emoji="👍",
        )
        react_result = await chat(react_request)

        assert react_result.action == "react"
        assert react_result.error is None
        assert react_result.react is not None
        assert react_result.react.added is True

    @pytest.mark.asyncio
    async def test_get_history_action(self, mock_chat_root, sample_group_with_messages):
        """Test get_history action retrieves channel history."""
        request = ChatInput(action="get_history", channel_id=sample_group_with_messages)
        result = await chat(request)

        assert result.action == "get_history"
        assert result.error is None
        assert result.get_history is not None
        assert len(result.get_history.messages) >= 0

    @pytest.mark.asyncio
    async def test_get_history_missing_channel(self):
        """Test get_history with missing channel_id."""
        request = ChatInput(action="get_history")
        result = await chat(request)

        assert result.action == "get_history"
        assert result.error is not None
        assert "channel_id" in result.error.lower()

    @pytest.mark.asyncio
    async def test_list_users_action(self, mock_chat_root, sample_group_basic):
        """Test list_users action."""
        request = ChatInput(action="list_users")
        result = await chat(request)

        assert result.action == "list_users"
        assert result.error is None
        assert result.list_users is not None

    @pytest.mark.asyncio
    async def test_delete_action(
        self, mock_chat_root, sample_group_basic, mock_current_user
    ):
        """Test delete action soft-deletes a message."""
        # First post a message
        post_request = ChatInput(
            action="post", channel_id=sample_group_basic, message="Delete me"
        )
        post_result = await chat(post_request)
        message_id = post_result.post.message_id

        # Delete it
        delete_request = ChatInput(
            action="delete", channel_id=sample_group_basic, post_id=message_id
        )
        delete_result = await chat(delete_request)

        assert delete_result.action == "delete"
        assert delete_result.error is None
        assert delete_result.delete is not None
        assert delete_result.delete.deleted is True


class TestChatSchema:
    """Test the chat_schema tool."""

    def test_get_input_schema(self):
        """Test getting input schema."""
        request = SchemaInput(model="input")
        result = chat_schema(request)

        assert result.model == "input"
        assert "properties" in result.json_schema
        assert "action" in result.json_schema["properties"]

    def test_get_output_schema(self):
        """Test getting output schema."""
        request = SchemaInput(model="output")
        result = chat_schema(request)

        assert result.model == "output"
        assert "properties" in result.json_schema

    def test_get_result_schema(self):
        """Test getting a result type schema."""
        request = SchemaInput(model="ChannelsResult")
        result = chat_schema(request)

        assert result.model == "ChannelsResult"
        assert "properties" in result.json_schema

    def test_unknown_model(self):
        """Test unknown model returns error."""
        request = SchemaInput(model="UnknownModel")
        result = chat_schema(request)

        assert result.model == "UnknownModel"
        assert "error" in result.json_schema


class TestChatHelp:
    """Test the CHAT_HELP constant."""

    def test_help_structure(self):
        """Test that help has proper structure."""
        assert CHAT_HELP.tool_name == "chat"
        assert CHAT_HELP.description
        assert len(CHAT_HELP.actions) > 0

    def test_all_actions_have_info(self):
        """Test that all actions have required/optional params."""
        for _action_name, action_info in CHAT_HELP.actions.items():
            assert action_info.description
            assert isinstance(action_info.required_params, list)
            assert isinstance(action_info.optional_params, list)
