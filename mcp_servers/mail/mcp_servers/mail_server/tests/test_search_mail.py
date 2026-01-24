"""Unit tests for search_mail tool."""

import sys
from pathlib import Path

import pytest

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.search_mail import search_mail
from tools.send_mail import send_mail

from .helpers.assertions import (
    assert_mail_count,
    assert_mail_in_list,
    assert_mail_sent,
    assert_search_results,
)


class TestSearchMail:
    """Test mail search functionality."""

    @pytest.mark.asyncio
    async def test_search_empty_mailbox(self, mock_mail_root):
        """Test searching in empty mailbox."""
        result = await search_mail(subject="test")
        assert_mail_count(result, 0)

    @pytest.mark.asyncio
    async def test_search_by_subject(self, mock_mail_root):
        """Test searching by subject."""
        # Send mails with different subjects
        mail1_result = await send_mail(
            from_email="sender@example.com",
            to_email="recipient@example.com",
            subject="Project Update",
            body="Body 1",
        )
        mail1_id = assert_mail_sent(mail1_result)

        await send_mail(
            from_email="sender@example.com",
            to_email="recipient@example.com",
            subject="Random Topic",
            body="Body 2",
        )

        # Search for "Project"
        result = await search_mail(subject="Project")
        assert_search_results(result, 1)
        assert_mail_in_list(result, mail1_id)

    @pytest.mark.asyncio
    async def test_search_by_from_email(self, mock_mail_root):
        """Test searching by sender."""
        mail1_result = await send_mail(
            from_email="alice@example.com",
            to_email="recipient@example.com",
            subject="Test 1",
            body="Body 1",
        )
        mail1_id = assert_mail_sent(mail1_result)

        await send_mail(
            from_email="bob@example.com",
            to_email="recipient@example.com",
            subject="Test 2",
            body="Body 2",
        )

        # Search for alice
        result = await search_mail(from_email="alice")
        assert_search_results(result, 1)
        assert_mail_in_list(result, mail1_id)

    @pytest.mark.asyncio
    async def test_search_by_to_email(self, mock_mail_root):
        """Test searching by recipient."""
        mail1_result = await send_mail(
            from_email="sender@example.com",
            to_email="alice@example.com",
            subject="Subject",
            body="Body content",
        )
        mail1_id = assert_mail_sent(mail1_result)

        await send_mail(
            from_email="sender@example.com",
            to_email="bob@example.com",
            subject="Subject",
            body="Body",
        )

        # Search for alice
        result = await search_mail(to_email="alice")
        assert_search_results(result, 1)
        assert_mail_in_list(result, mail1_id)

    @pytest.mark.asyncio
    async def test_search_by_thread_id(self, mock_mail_root, sample_mail_with_thread):
        """Test searching by thread ID."""
        mail1_result = await send_mail(**sample_mail_with_thread)
        mail1_id = assert_mail_sent(mail1_result)

        # Send another mail without thread
        await send_mail(
            from_email="sender@example.com",
            to_email="recipient@example.com",
            subject="Unrelated",
            body="Random body",
        )

        # Search for specific thread
        result = await search_mail(thread_id=sample_mail_with_thread["thread_id"])
        assert_search_results(result, 1)
        assert_mail_in_list(result, mail1_id)

    @pytest.mark.asyncio
    async def test_search_no_results(self, mock_mail_root, sample_mail_basic):
        """Test search with no matching results."""
        await send_mail(**sample_mail_basic)

        result = await search_mail(subject="nonexistent_keyword_xyz")
        assert_mail_count(result, 0)

    @pytest.mark.asyncio
    async def test_search_multiple_results(self, mock_mail_root):
        """Test search returning multiple results."""
        mail_ids = []

        # Send multiple mails with same keyword
        for i in range(3):
            result = await send_mail(
                from_email="sender@example.com",
                to_email="recipient@example.com",
                subject=f"Important Email {i}",
                body=f"Body {i}",
            )
            mail_id = assert_mail_sent(result)
            mail_ids.append(mail_id)

        # Search for "Important"
        result = await search_mail(subject="Important")
        assert_search_results(result, 3)
        for mail_id in mail_ids:
            assert_mail_in_list(result, mail_id)

    @pytest.mark.asyncio
    async def test_search_with_limit(self, mock_mail_root, mock_list_pagination):
        """Test search with limit parameter."""
        # Send 10 mails with same keyword
        for i in range(10):
            await send_mail(
                from_email="sender@example.com",
                to_email="recipient@example.com",
                subject=f"Keyword Email {i}",
                body=f"Body {i}",
            )

        # Search with limit - should return exactly 5 results
        result = await search_mail(subject="Keyword", limit=5)
        assert_mail_count(result, 5)

    @pytest.mark.asyncio
    async def test_search_combined_filters(self, mock_mail_root):
        """Test search with multiple filters (AND logic)."""
        # Send mail that matches all criteria
        mail1_result = await send_mail(
            from_email="alice@example.com",
            to_email="bob@example.com",
            subject="Project Discussion",
            body="Body",
        )
        mail1_id = assert_mail_sent(mail1_result)

        # Send mail that matches only some criteria
        await send_mail(
            from_email="alice@example.com",
            to_email="charlie@example.com",
            subject="Random Topic",
            body="Body",
        )

        # Search with combined filters
        result = await search_mail(
            from_email="alice", to_email="bob", subject="Project"
        )
        assert_search_results(result, 1)
        assert_mail_in_list(result, mail1_id)
