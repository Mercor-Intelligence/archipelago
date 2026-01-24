"""Unit tests for send_mail tool."""

import sys
from pathlib import Path

import pytest

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.send_mail import send_mail

from .helpers.assertions import (
    assert_mail_sent,
    assert_recipients_count,
    assert_validation_error,
)


class TestSendMail:
    """Test email sending functionality."""

    @pytest.mark.asyncio
    async def test_send_basic_mail(self, mock_mail_root, sample_mail_basic):
        """Test sending a basic email."""
        result = await send_mail(**sample_mail_basic)
        mail_id = assert_mail_sent(result)
        assert mail_id
        assert_recipients_count(result, 1)

    @pytest.mark.asyncio
    async def test_send_to_multiple_recipients(
        self, mock_mail_root, sample_mail_multiple_recipients
    ):
        """Test sending email to multiple recipients."""
        result = await send_mail(**sample_mail_multiple_recipients)
        mail_id = assert_mail_sent(result)
        assert mail_id
        # 2 to + 1 cc = 3 total
        assert_recipients_count(result, 3)

    @pytest.mark.asyncio
    async def test_send_with_cc_bcc(self, mock_mail_root, sample_mail_with_cc_bcc):
        """Test sending email with CC and BCC."""
        result = await send_mail(**sample_mail_with_cc_bcc)
        mail_id = assert_mail_sent(result)
        assert mail_id
        # 1 to + 2 cc + 1 bcc = 4 total
        assert_recipients_count(result, 4)

    @pytest.mark.asyncio
    async def test_send_html_mail(self, mock_mail_root, sample_mail_html):
        """Test sending HTML formatted email."""
        result = await send_mail(**sample_mail_html)
        mail_id = assert_mail_sent(result)
        assert mail_id

    @pytest.mark.asyncio
    async def test_send_with_thread_id(self, mock_mail_root, sample_mail_with_thread):
        """Test sending email with thread ID."""
        result = await send_mail(**sample_mail_with_thread)
        mail_id = assert_mail_sent(result)
        assert mail_id

    @pytest.mark.asyncio
    async def test_send_invalid_from_email(self, mock_mail_root):
        """Test validation error for invalid from_email."""
        result = await send_mail(
            from_email="not-an-email",
            to_email="recipient@example.com",
            subject="Test",
            body="Body",
        )
        assert_validation_error(result, "from_email")

    @pytest.mark.asyncio
    async def test_send_invalid_to_email(self, mock_mail_root):
        """Test validation error for invalid to_email."""
        result = await send_mail(
            from_email="sender@example.com",
            to_email="not-an-email",
            subject="Test",
            body="Body",
        )
        assert_validation_error(result, "to_email")

    @pytest.mark.asyncio
    async def test_send_empty_to_list(self, mock_mail_root):
        """Test validation error for empty to_email list."""
        result = await send_mail(
            from_email="sender@example.com",
            to_email=[],
            subject="Test",
            body="Body",
        )
        assert_validation_error(result, "to_email")

    @pytest.mark.asyncio
    async def test_send_invalid_cc_email(self, mock_mail_root):
        """Test validation error for invalid CC email."""
        result = await send_mail(
            from_email="sender@example.com",
            to_email="recipient@example.com",
            subject="Test",
            body="Body",
            cc="not-an-email",
        )
        assert_validation_error(result, "cc")

    @pytest.mark.asyncio
    async def test_send_invalid_bcc_email(self, mock_mail_root):
        """Test validation error for invalid BCC email."""
        result = await send_mail(
            from_email="sender@example.com",
            to_email="recipient@example.com",
            subject="Test",
            body="Body",
            bcc=["valid@example.com", "not-an-email"],
        )
        assert_validation_error(result, "bcc")

    @pytest.mark.asyncio
    async def test_send_empty_subject(self, mock_mail_root):
        """Test sending email with empty subject."""
        result = await send_mail(
            from_email="sender@example.com",
            to_email="recipient@example.com",
            subject="",
            body="Body",
        )
        # Empty subject should be rejected
        assert_validation_error(result, "subject")

    @pytest.mark.asyncio
    async def test_send_empty_body(self, mock_mail_root):
        """Test sending email with empty body."""
        result = await send_mail(
            from_email="sender@example.com",
            to_email="recipient@example.com",
            subject="Test",
            body="",
        )
        # Empty body should be allowed
        mail_id = assert_mail_sent(result)
        assert mail_id

    @pytest.mark.asyncio
    async def test_send_very_long_subject(self, mock_mail_root):
        """Test sending email with very long subject."""
        result = await send_mail(
            from_email="sender@example.com",
            to_email="recipient@example.com",
            subject="x" * 1000,  # Very long subject (exceeds MAX_SUBJECT_LENGTH of 998)
            body="Body",
        )
        # Should fail validation as subject exceeds MAX_SUBJECT_LENGTH
        assert_validation_error(result, "subject")
