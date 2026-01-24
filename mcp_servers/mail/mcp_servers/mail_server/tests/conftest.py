"""Shared fixtures for mail server tests."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture
def temp_mail_root(tmp_path):
    """Create a temporary mail data directory.

    Args:
        tmp_path: pytest's temporary directory fixture

    Yields:
        Path: Path to the temporary mail directory
    """
    mail_dir = tmp_path / "mail"
    mail_dir.mkdir(exist_ok=True)
    yield mail_dir


@pytest.fixture
def mock_mail_root(temp_mail_root):
    """Mock the MAIL_DATA_ROOT to point to temp directory.

    Args:
        temp_mail_root: Temporary mail directory fixture

    Yields:
        str: Path to the temporary mail directory as string
    """
    with patch("utils.config.MAIL_DATA_ROOT", str(temp_mail_root)):
        with patch("utils.path.MAIL_DATA_ROOT", str(temp_mail_root)):
            yield str(temp_mail_root)


@pytest.fixture
def sample_mail_basic():
    """Return a basic email sample."""
    return {
        "from_email": "sender@example.com",
        "to_email": "recipient@example.com",
        "subject": "Test Email",
        "body": "This is a test email body.",
    }


@pytest.fixture
def sample_mail_multiple_recipients():
    """Return email with multiple recipients."""
    return {
        "from_email": "sender@example.com",
        "to_email": ["recipient1@example.com", "recipient2@example.com"],
        "subject": "Team Update",
        "body": "Update for the team.",
        "cc": "manager@example.com",
    }


@pytest.fixture
def sample_mail_with_cc_bcc():
    """Return email with CC and BCC."""
    return {
        "from_email": "sender@example.com",
        "to_email": "recipient@example.com",
        "subject": "Confidential Update",
        "body": "Confidential information.",
        "cc": ["cc1@example.com", "cc2@example.com"],
        "bcc": "bcc@example.com",
    }


@pytest.fixture
def sample_mail_html():
    """Return email with HTML body."""
    return {
        "from_email": "sender@example.com",
        "to_email": "recipient@example.com",
        "subject": "HTML Newsletter",
        "body": "<html><body><h1>Hello!</h1><p>This is HTML content.</p></body></html>",
        "body_format": "html",
    }


@pytest.fixture
def sample_mail_with_thread():
    """Return email with thread information."""
    return {
        "from_email": "sender@example.com",
        "to_email": "recipient@example.com",
        "subject": "Discussion Topic",
        "body": "Let's discuss this.",
        "thread_id": "<thread-123@example.com>",
    }


@pytest.fixture
def mock_list_pagination():
    """Mock list pagination limits."""
    with patch("tools.list_mails.DEFAULT_LIST_LIMIT", 10):
        with patch("tools.list_mails.MAX_LIST_LIMIT", 20):
            with patch("tools.search_mail.DEFAULT_LIST_LIMIT", 10):
                with patch("tools.search_mail.MAX_LIST_LIMIT", 20):
                    yield
