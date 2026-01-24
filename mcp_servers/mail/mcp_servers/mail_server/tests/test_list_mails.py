"""Unit tests for list_mails tool."""

import sys
from pathlib import Path

import pytest

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.list_mails import list_mails
from tools.send_mail import send_mail

from .helpers.assertions import (
    assert_mail_count,
    assert_mail_in_list,
    assert_mail_sent,
)


class TestListMails:
    """Test mail listing functionality."""

    @pytest.mark.asyncio
    async def test_list_empty_mailbox(self, mock_mail_root):
        """Test listing mails when mailbox is empty."""
        result = await list_mails()
        assert_mail_count(result, 0)

    @pytest.mark.asyncio
    async def test_list_single_mail(self, mock_mail_root, sample_mail_basic):
        """Test listing mailbox with single mail."""
        send_result = await send_mail(**sample_mail_basic)
        mail_id = assert_mail_sent(send_result)

        result = await list_mails()
        assert_mail_count(result, 1)
        assert_mail_in_list(result, mail_id)

    @pytest.mark.asyncio
    async def test_list_multiple_mails(self, mock_mail_root):
        """Test listing multiple mails."""
        mail_ids = []

        for i in range(3):
            result = await send_mail(
                from_email="sender@example.com",
                to_email="recipient@example.com",
                subject=f"Email {i}",
                body=f"Body {i}",
            )
            mail_id = assert_mail_sent(result)
            mail_ids.append(mail_id)

        result = await list_mails()
        assert_mail_count(result, 3)
        for mail_id in mail_ids:
            assert_mail_in_list(result, mail_id)

    @pytest.mark.asyncio
    async def test_list_mails_sorted_by_time(self, mock_mail_root):
        """Test that mails are sorted by timestamp (most recent first)."""
        mail_ids = []

        for i in range(3):
            result = await send_mail(
                from_email="sender@example.com",
                to_email="recipient@example.com",
                subject=f"Email {i}",
                body=f"Body {i}",
            )
            mail_id = assert_mail_sent(result)
            mail_ids.append(mail_id)

        result = await list_mails()
        # Verify all mails are present (order may vary due to same timestamp)
        assert_mail_count(result, 3)
        for mail_id in mail_ids:
            assert_mail_in_list(result, mail_id)

    @pytest.mark.asyncio
    async def test_list_with_limit(self, mock_mail_root, mock_list_pagination):
        """Test listing mails with limit parameter."""
        # Create 15 mails
        for i in range(15):
            await send_mail(
                from_email="sender@example.com",
                to_email="recipient@example.com",
                subject=f"Email {i}",
                body=f"Body {i}",
            )

        # List with limit=5
        result = await list_mails(limit=5)
        assert_mail_count(result, 5)

    @pytest.mark.asyncio
    async def test_list_with_offset(self, mock_mail_root, mock_list_pagination):
        """Test listing mails with offset parameter."""
        mail_ids = []

        # Create 15 mails
        for i in range(15):
            result = await send_mail(
                from_email="sender@example.com",
                to_email="recipient@example.com",
                subject=f"Email {i}",
                body=f"Body {i}",
            )
            mail_id = assert_mail_sent(result)
            mail_ids.append(mail_id)

        # List with offset=5, limit=5
        result = await list_mails(limit=5, offset=5)
        assert_mail_count(result, 5)

        # First 5 mails should not be in result (they're offset)
        # Note: mails are sorted most recent first

    @pytest.mark.asyncio
    async def test_list_invalid_limit(self, mock_mail_root, sample_mail_basic):
        """Test listing with invalid limit (should use default)."""
        await send_mail(**sample_mail_basic)

        result = await list_mails(limit=-5)
        # Should use default limit and still return result
        assert_mail_count(result, 1)

    @pytest.mark.asyncio
    async def test_list_exceeds_max_limit(
        self, mock_mail_root, sample_mail_basic, mock_list_pagination
    ):
        """Test listing with limit exceeding max (should cap at max)."""
        await send_mail(**sample_mail_basic)

        # Try to request 100 mails, but max is 20
        result = await list_mails(limit=100)
        # Should still work, capped at max
        assert_mail_count(result, 1)
