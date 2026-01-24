"""Unit tests for reply_all_mail tool."""

import sys
from pathlib import Path

import pytest

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.read_mail import read_mail
from tools.reply_all_mail import reply_all_mail
from tools.send_mail import send_mail

from .helpers.assertions import (
    assert_mail_not_found,
    assert_mail_sent,
    assert_subject_prefix,
)


class TestReplyAllMail:
    """Test reply-all mail functionality."""

    @pytest.mark.asyncio
    async def test_reply_all_to_mail(
        self, mock_mail_root, sample_mail_multiple_recipients
    ):
        """Test replying to all recipients."""
        # Send original mail with multiple recipients
        send_result = await send_mail(**sample_mail_multiple_recipients)
        original_mail_id = assert_mail_sent(send_result)

        # Reply to all
        result = await reply_all_mail(
            original_mail_id=original_mail_id, body="Reply to everyone"
        )
        reply_mail_id = assert_mail_sent(result)
        assert reply_mail_id != original_mail_id

    @pytest.mark.asyncio
    async def test_reply_all_adds_re_prefix(
        self, mock_mail_root, sample_mail_multiple_recipients
    ):
        """Test that reply_all adds 'Re:' prefix to subject."""
        send_result = await send_mail(**sample_mail_multiple_recipients)
        original_mail_id = assert_mail_sent(send_result)

        result = await reply_all_mail(
            original_mail_id=original_mail_id, body="Reply all body"
        )
        reply_mail_id = assert_mail_sent(result)

        # Read the reply to check subject
        read_result = await read_mail(reply_mail_id)
        assert_subject_prefix(read_result, "Re:")

    @pytest.mark.asyncio
    async def test_reply_all_includes_cc(self, mock_mail_root, sample_mail_with_cc_bcc):
        """Test that reply_all includes CC recipients."""
        send_result = await send_mail(**sample_mail_with_cc_bcc)
        original_mail_id = assert_mail_sent(send_result)

        result = await reply_all_mail(
            original_mail_id=original_mail_id, body="Reply to all"
        )
        reply_mail_id = assert_mail_sent(result)

        # Read the reply to verify recipients
        read_result = await read_mail(reply_mail_id)
        # Should include original CC addresses
        assert "cc1@example.com" in read_result or "cc2@example.com" in read_result

    @pytest.mark.asyncio
    async def test_reply_all_to_nonexistent_mail(self, mock_mail_root):
        """Test reply_all to non-existent mail."""
        result = await reply_all_mail(
            original_mail_id="<nonexistent@example.com>", body="Reply all body"
        )
        assert_mail_not_found(result)

    @pytest.mark.asyncio
    async def test_reply_all_with_html_body(
        self, mock_mail_root, sample_mail_multiple_recipients
    ):
        """Test reply_all with HTML formatted body."""
        send_result = await send_mail(**sample_mail_multiple_recipients)
        original_mail_id = assert_mail_sent(send_result)

        result = await reply_all_mail(
            original_mail_id=original_mail_id,
            body="<html><body><p>HTML reply all</p></body></html>",
            body_format="html",
        )
        reply_mail_id = assert_mail_sent(result)
        assert reply_mail_id
