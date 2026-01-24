"""Unit tests for forward_mail tool."""

import sys
from pathlib import Path

import pytest

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.forward_mail import forward_mail
from tools.read_mail import read_mail
from tools.send_mail import send_mail

from .helpers.assertions import (
    assert_mail_field,
    assert_mail_not_found,
    assert_mail_sent,
    assert_subject_prefix,
)


class TestForwardMail:
    """Test mail forwarding functionality."""

    @pytest.mark.asyncio
    async def test_forward_mail_basic(self, mock_mail_root, sample_mail_basic):
        """Test forwarding an email."""
        # Send original mail
        send_result = await send_mail(**sample_mail_basic)
        original_mail_id = assert_mail_sent(send_result)

        # Forward it
        result = await forward_mail(
            original_mail_id=original_mail_id, to_email="newrecipient@example.com"
        )
        forward_mail_id = assert_mail_sent(result)
        assert forward_mail_id != original_mail_id

    @pytest.mark.asyncio
    async def test_forward_adds_fwd_prefix(self, mock_mail_root, sample_mail_basic):
        """Test that forward adds 'Fwd:' prefix to subject."""
        send_result = await send_mail(**sample_mail_basic)
        original_mail_id = assert_mail_sent(send_result)

        result = await forward_mail(
            original_mail_id=original_mail_id, to_email="newrecipient@example.com"
        )
        forward_mail_id = assert_mail_sent(result)

        # Read the forwarded mail to check subject
        read_result = await read_mail(forward_mail_id)
        assert_subject_prefix(read_result, "Fwd:")

    @pytest.mark.asyncio
    async def test_forward_with_additional_body(
        self, mock_mail_root, sample_mail_basic
    ):
        """Test forwarding with additional body text."""
        send_result = await send_mail(**sample_mail_basic)
        original_mail_id = assert_mail_sent(send_result)

        additional_text = "FYI - Please review this email."
        result = await forward_mail(
            original_mail_id=original_mail_id,
            to_email="newrecipient@example.com",
            body=additional_text,
        )
        forward_mail_id = assert_mail_sent(result)

        # Read the forwarded mail to check additional body
        read_result = await read_mail(forward_mail_id)
        assert_mail_field(read_result, "Body", additional_text)

    @pytest.mark.asyncio
    async def test_forward_to_multiple_recipients(
        self, mock_mail_root, sample_mail_basic
    ):
        """Test forwarding to multiple recipients."""
        send_result = await send_mail(**sample_mail_basic)
        original_mail_id = assert_mail_sent(send_result)

        result = await forward_mail(
            original_mail_id=original_mail_id,
            to_email=["recipient1@example.com", "recipient2@example.com"],
            cc="cc@example.com",
        )
        forward_mail_id = assert_mail_sent(result)
        assert forward_mail_id

    @pytest.mark.asyncio
    async def test_forward_nonexistent_mail(self, mock_mail_root):
        """Test forwarding non-existent mail."""
        result = await forward_mail(
            original_mail_id="<nonexistent@example.com>",
            to_email="recipient@example.com",
        )
        assert_mail_not_found(result)

    @pytest.mark.asyncio
    async def test_forward_invalid_to_email(self, mock_mail_root, sample_mail_basic):
        """Test forwarding with invalid recipient email."""
        send_result = await send_mail(**sample_mail_basic)
        original_mail_id = assert_mail_sent(send_result)

        result = await forward_mail(
            original_mail_id=original_mail_id, to_email="not-an-email"
        )
        # Should fail with validation error
        assert "invalid" in result.lower() or "error" in result.lower()
