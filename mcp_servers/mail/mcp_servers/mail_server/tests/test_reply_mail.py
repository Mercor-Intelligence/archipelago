"""Unit tests for reply_mail tool."""

import sys
from pathlib import Path

import pytest

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.read_mail import read_mail
from tools.reply_mail import reply_mail
from tools.send_mail import send_mail

from .helpers.assertions import (
    assert_mail_not_found,
    assert_mail_sent,
    assert_subject_prefix,
    assert_thread_preserved,
)


class TestReplyMail:
    """Test reply mail functionality."""

    @pytest.mark.asyncio
    async def test_reply_to_mail(self, mock_mail_root, sample_mail_basic):
        """Test replying to an email."""
        # Send original mail
        send_result = await send_mail(**sample_mail_basic)
        original_mail_id = assert_mail_sent(send_result)

        # Reply to it
        result = await reply_mail(
            original_mail_id=original_mail_id, body="This is my reply."
        )
        reply_mail_id = assert_mail_sent(result)
        assert reply_mail_id != original_mail_id

    @pytest.mark.asyncio
    async def test_reply_adds_re_prefix(self, mock_mail_root, sample_mail_basic):
        """Test that reply adds 'Re:' prefix to subject."""
        send_result = await send_mail(**sample_mail_basic)
        original_mail_id = assert_mail_sent(send_result)

        result = await reply_mail(original_mail_id=original_mail_id, body="Reply body")
        reply_mail_id = assert_mail_sent(result)

        # Read the reply to check subject
        read_result = await read_mail(reply_mail_id)
        assert_subject_prefix(read_result, "Re:")

    @pytest.mark.asyncio
    async def test_reply_preserves_thread(
        self, mock_mail_root, sample_mail_with_thread
    ):
        """Test that reply preserves thread information."""
        send_result = await send_mail(**sample_mail_with_thread)
        original_mail_id = assert_mail_sent(send_result)

        result = await reply_mail(
            original_mail_id=original_mail_id, body="Reply in thread"
        )
        reply_mail_id = assert_mail_sent(result)

        # Read the reply to check thread_id
        read_result = await read_mail(reply_mail_id)
        assert_thread_preserved(read_result, sample_mail_with_thread["thread_id"])

    @pytest.mark.asyncio
    async def test_reply_to_nonexistent_mail(self, mock_mail_root):
        """Test replying to non-existent mail."""
        result = await reply_mail(
            original_mail_id="<nonexistent@example.com>", body="Reply body"
        )
        assert_mail_not_found(result)

    @pytest.mark.asyncio
    async def test_reply_with_html_body(self, mock_mail_root, sample_mail_basic):
        """Test replying with HTML formatted body."""
        send_result = await send_mail(**sample_mail_basic)
        original_mail_id = assert_mail_sent(send_result)

        result = await reply_mail(
            original_mail_id=original_mail_id,
            body="<html><body><p>HTML reply</p></body></html>",
            body_format="html",
        )
        reply_mail_id = assert_mail_sent(result)
        assert reply_mail_id

    @pytest.mark.asyncio
    async def test_reply_empty_body(self, mock_mail_root, sample_mail_basic):
        """Test replying with empty body."""
        send_result = await send_mail(**sample_mail_basic)
        original_mail_id = assert_mail_sent(send_result)

        result = await reply_mail(original_mail_id=original_mail_id, body="")
        # Empty body should be allowed
        mail_id = assert_mail_sent(result)
        assert mail_id
