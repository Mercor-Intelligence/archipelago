"""Integration tests for mail operations."""

import sys
from pathlib import Path

import pytest

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.forward_mail import forward_mail
from tools.list_mails import list_mails
from tools.read_mail import read_mail
from tools.reply_all_mail import reply_all_mail
from tools.reply_mail import reply_mail
from tools.search_mail import search_mail
from tools.send_mail import send_mail

from .helpers.assertions import (
    assert_mail_count,
    assert_mail_field,
    assert_mail_in_list,
    assert_mail_sent,
    assert_search_results,
    assert_subject_prefix,
    assert_thread_preserved,
)


class TestIntegration:
    """Test integrated mail operations."""

    @pytest.mark.asyncio
    async def test_send_read_workflow(self, mock_mail_root, sample_mail_basic):
        """Test send → read workflow."""
        # Send
        send_result = await send_mail(**sample_mail_basic)
        mail_id = assert_mail_sent(send_result)

        # Read
        read_result = await read_mail(mail_id)
        assert_mail_field(read_result, "Subject", sample_mail_basic["subject"])
        assert_mail_field(read_result, "Body", sample_mail_basic["body"])

        # List
        list_result = await list_mails()
        assert_mail_count(list_result, 1)
        assert_mail_in_list(list_result, mail_id)

    @pytest.mark.asyncio
    async def test_email_thread_workflow(self, mock_mail_root, sample_mail_with_thread):
        """Test complete email thread: send → reply → reply_all."""
        # Send original
        send_result = await send_mail(**sample_mail_with_thread)
        original_mail_id = assert_mail_sent(send_result)

        # Reply
        reply_result = await reply_mail(
            original_mail_id=original_mail_id, body="First reply"
        )
        reply_mail_id = assert_mail_sent(reply_result)

        # Verify thread preserved
        reply_read = await read_mail(reply_mail_id)
        assert_thread_preserved(reply_read, sample_mail_with_thread["thread_id"])
        assert_subject_prefix(reply_read, "Re:")

        # Reply all to the reply
        reply_all_result = await reply_all_mail(
            original_mail_id=reply_mail_id, body="Reply to reply"
        )
        _ = assert_mail_sent(reply_all_result)

        # List all mails in thread
        list_result = await list_mails()
        assert_mail_count(list_result, 3)

    @pytest.mark.asyncio
    async def test_forward_workflow(self, mock_mail_root, sample_mail_basic):
        """Test send → forward workflow."""
        # Send original
        send_result = await send_mail(**sample_mail_basic)
        original_mail_id = assert_mail_sent(send_result)

        # Forward
        forward_result = await forward_mail(
            original_mail_id=original_mail_id,
            to_email="newperson@example.com",
            body="FYI",
        )
        forward_mail_id = assert_mail_sent(forward_result)

        # Read forwarded mail
        forward_read = await read_mail(forward_mail_id)
        assert_subject_prefix(forward_read, "Fwd:")
        assert_mail_field(forward_read, "Body", "FYI")

    @pytest.mark.asyncio
    async def test_search_across_operations(self, mock_mail_root):
        """Test search after various mail operations."""
        # Send original
        send_result = await send_mail(
            from_email="sender@example.com",
            to_email="recipient@example.com",
            subject="Urgent Project",
            body="Original body",
        )
        original_mail_id = assert_mail_sent(send_result)

        # Reply
        reply_result = await reply_mail(
            original_mail_id=original_mail_id, body="Got it, working on Urgent Project"
        )
        reply_mail_id = assert_mail_sent(reply_result)

        # Forward
        forward_result = await forward_mail(
            original_mail_id=original_mail_id,
            to_email="other@example.com",
            body="Urgent Project needs attention",
        )
        forward_mail_id = assert_mail_sent(forward_result)

        # Search for "Urgent Project"
        search_result = await search_mail(subject="Urgent Project")
        assert_search_results(search_result, 3)
        assert_mail_in_list(search_result, original_mail_id)
        assert_mail_in_list(search_result, reply_mail_id)
        assert_mail_in_list(search_result, forward_mail_id)

    @pytest.mark.asyncio
    async def test_complex_conversation(
        self, mock_mail_root, sample_mail_multiple_recipients
    ):
        """Test complex conversation with multiple operations."""
        # Send original to multiple recipients
        send_result = await send_mail(**sample_mail_multiple_recipients)
        original_mail_id = assert_mail_sent(send_result)

        # Reply from one person
        reply1_result = await reply_mail(
            original_mail_id=original_mail_id, body="I agree"
        )
        _ = assert_mail_sent(reply1_result)

        # Reply all from another
        reply2_result = await reply_all_mail(
            original_mail_id=original_mail_id, body="I also agree"
        )
        reply2_mail_id = assert_mail_sent(reply2_result)

        # Forward the conversation
        forward_result = await forward_mail(
            original_mail_id=reply2_mail_id,
            to_email="external@example.com",
            body="See the agreement below",
        )
        _ = assert_mail_sent(forward_result)

        # List all mails
        list_result = await list_mails()
        assert_mail_count(list_result, 4)

        search_result = await search_mail(subject="Team Update")
        assert_search_results(search_result, 4)
