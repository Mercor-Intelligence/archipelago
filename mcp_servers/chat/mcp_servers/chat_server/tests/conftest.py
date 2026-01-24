"""Shared fixtures for chat server tests."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture
def temp_chat_root(tmp_path):
    """Create a temporary chat data directory.

    Args:
        tmp_path: pytest's temporary directory fixture

    Yields:
        Path: Path to the temporary chat directory
    """
    chat_dir = tmp_path / "chat"
    chat_dir.mkdir(exist_ok=True)
    yield chat_dir


@pytest.fixture
def mock_chat_root(temp_chat_root):
    """Mock the CHAT_DATA_ROOT to point to temp directory.

    Args:
        temp_chat_root: Temporary chat directory fixture

    Yields:
        str: Path to the temporary chat directory as string
    """
    with patch("utils.config.CHAT_DATA_ROOT", str(temp_chat_root)):
        with patch("utils.path.CHAT_DATA_ROOT", str(temp_chat_root)):
            yield str(temp_chat_root)


@pytest.fixture
def mock_current_user():
    """Mock the current user configuration."""
    with patch("utils.config.CURRENT_USER_EMAIL", "test@example.com"):
        with patch("utils.config.CURRENT_USER_ID", "User 123456789012345678901"):
            with patch("tools.post_message.CURRENT_USER_EMAIL", "test@example.com"):
                with patch(
                    "tools.reply_to_thread.CURRENT_USER_EMAIL", "test@example.com"
                ):
                    with patch(
                        "tools.add_reaction.CURRENT_USER_EMAIL", "test@example.com"
                    ):
                        yield


@pytest.fixture
def sample_group_basic(mock_chat_root):
    """Create a basic group with no messages."""
    group_id = "Space AAAAAAAAAAA"
    groups_dir = Path(mock_chat_root) / "Groups" / group_id
    groups_dir.mkdir(parents=True, exist_ok=True)

    group_info = {
        "name": "Test Group",
        "members": [
            {"name": "Test User", "email": "test@example.com", "user_type": "Human"},
            {"name": "Other User", "email": "other@example.com", "user_type": "Human"},
        ],
    }

    with open(groups_dir / "group_info.json", "w") as f:
        json.dump(group_info, f)

    messages = {"messages": []}
    with open(groups_dir / "messages.json", "w") as f:
        json.dump(messages, f)

    return group_id


@pytest.fixture
def sample_group_with_messages(mock_chat_root):
    """Create a group with existing messages."""
    group_id = "Space BBBBBBBBBBB"
    groups_dir = Path(mock_chat_root) / "Groups" / group_id
    groups_dir.mkdir(parents=True, exist_ok=True)

    group_info = {
        "name": "Chat Group",
        "members": [
            {"name": "Test User", "email": "test@example.com", "user_type": "Human"},
            {"name": "Alice", "email": "alice@example.com", "user_type": "Human"},
        ],
    }

    with open(groups_dir / "group_info.json", "w") as f:
        json.dump(group_info, f)

    messages = {
        "messages": [
            {
                "creator": {
                    "name": "Alice",
                    "email": "alice@example.com",
                    "user_type": "Human",
                },
                "created_date": "Monday, December 8, 2025 at 10:00:00 AM UTC",
                "text": "Hello everyone!",
                "topic_id": "topicabc",
                "message_id": "BBBBBBBBBBB/topicabc/topicabc",
                "reactions": [],
                "annotations": [],
            }
        ]
    }

    with open(groups_dir / "messages.json", "w") as f:
        json.dump(messages, f)

    return group_id


@pytest.fixture
def sample_multiple_groups(mock_chat_root):
    """Create multiple groups for pagination testing."""
    group_ids = []

    for i in range(5):
        group_id = f"Space TEST{i:07d}"
        groups_dir = Path(mock_chat_root) / "Groups" / group_id
        groups_dir.mkdir(parents=True, exist_ok=True)

        group_info = {
            "name": f"Group {i}",
            "members": [
                {"name": "Test User", "email": "test@example.com", "user_type": "Human"}
            ],
        }

        with open(groups_dir / "group_info.json", "w") as f:
            json.dump(group_info, f)

        messages = {"messages": []}
        with open(groups_dir / "messages.json", "w") as f:
            json.dump(messages, f)

        group_ids.append(group_id)

    return group_ids


@pytest.fixture
def mock_pagination_limits():
    """Mock pagination limits for testing."""
    with patch("utils.config.DEFAULT_GROUPS_LIMIT", 10):
        with patch("utils.config.MAX_GROUPS_LIMIT", 20):
            with patch("utils.config.DEFAULT_MESSAGES_LIMIT", 5):
                with patch("utils.config.MAX_MESSAGES_LIMIT", 10):
                    yield
