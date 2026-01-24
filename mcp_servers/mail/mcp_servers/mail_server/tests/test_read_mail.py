"""Unit tests for read_mail tool."""

import sys
from pathlib import Path

import pytest

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.read_mail import read_mail
from tools.send_mail import send_mail

from .helpers.assertions import (
    assert_mail_field,
    assert_mail_not_found,
    assert_mail_sent,
)


class TestReadMail:
    """Test mail reading functionality."""

    @pytest.mark.asyncio
    async def test_read_existing_mail(self, mock_mail_root, sample_mail_basic):
        """Test reading an existing email."""
        # First send an email
        send_result = await send_mail(**sample_mail_basic)
        mail_id = assert_mail_sent(send_result)

        # Then read it
        result = await read_mail(mail_id)
        assert_mail_field(result, "From", sample_mail_basic["from_email"])
        assert_mail_field(result, "To", sample_mail_basic["to_email"])
        assert_mail_field(result, "Subject", sample_mail_basic["subject"])
        assert_mail_field(result, "Body", sample_mail_basic["body"])

    @pytest.mark.asyncio
    async def test_read_mail_with_cc(self, mock_mail_root, sample_mail_with_cc_bcc):
        """Test reading mail with CC and BCC."""
        send_result = await send_mail(**sample_mail_with_cc_bcc)
        mail_id = assert_mail_sent(send_result)

        result = await read_mail(mail_id)
        assert_mail_field(result, "Cc", "cc1@example.com")
        assert_mail_field(result, "Bcc", "bcc@example.com")

    @pytest.mark.asyncio
    async def test_read_html_mail(self, mock_mail_root, sample_mail_html):
        """Test reading HTML formatted mail."""
        send_result = await send_mail(**sample_mail_html)
        mail_id = assert_mail_sent(send_result)

        result = await read_mail(mail_id)
        assert "<h1>Hello!</h1>" in result or "Hello!" in result

    @pytest.mark.asyncio
    async def test_read_nonexistent_mail(self, mock_mail_root):
        """Test reading non-existent mail."""
        result = await read_mail("<nonexistent123@example.com>")
        assert_mail_not_found(result)

    @pytest.mark.asyncio
    async def test_read_mail_empty_id(self, mock_mail_root):
        """Test reading mail with empty ID."""
        result = await read_mail("")
        assert "invalid" in result.lower() or "failed" in result.lower()

    @pytest.mark.asyncio
    async def test_read_mail_invalid_id_format(self, mock_mail_root):
        """Test reading mail with invalid ID format."""
        result = await read_mail("not-a-valid-mail-id")
        # Should fail gracefully
        assert isinstance(result, str)
