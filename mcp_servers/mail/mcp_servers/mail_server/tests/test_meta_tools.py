"""Unit tests for meta-tools (mail and mail_schema)."""

import sys
from pathlib import Path

import pytest

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from tools._meta_tools import (
    MailInput,
    MailListResult,
    MailSummaryItem,
    SchemaInput,
    SendResult,
    mail,
    mail_schema,
)


class TestMailMetaTool:
    """Test the mail meta-tool."""

    @pytest.mark.asyncio
    async def test_help_action(self, mock_mail_root):
        """Test help action returns help info."""
        request = MailInput(action="help")
        result = await mail(request)

        assert result.action == "help"
        assert result.error is None
        assert result.help is not None
        assert result.help.tool_name == "mail"
        assert "send" in result.help.actions
        assert "read" in result.help.actions
        assert "list" in result.help.actions
        assert "search" in result.help.actions
        assert "reply" in result.help.actions
        assert "reply_all" in result.help.actions
        assert "forward" in result.help.actions

    @pytest.mark.asyncio
    async def test_send_action_success(self, mock_mail_root):
        """Test successful mail sending via meta-tool."""
        request = MailInput(
            action="send",
            from_email="sender@example.com",
            to_email="recipient@example.com",
            subject="Test Subject",
            body="Test body content",
        )
        result = await mail(request)

        assert result.action == "send"
        assert result.error is None
        assert result.send is not None
        assert result.send.success is True
        assert result.send.mail_id is not None
        assert result.send.error is None

    @pytest.mark.asyncio
    async def test_send_action_missing_to_email(self, mock_mail_root):
        """Test send action fails when to_email is missing."""
        request = MailInput(
            action="send",
            from_email="sender@example.com",
            subject="Test Subject",
            body="Test body",
        )
        result = await mail(request)

        assert result.action == "send"
        assert result.error is not None
        assert "to_email" in result.error.lower()

    @pytest.mark.asyncio
    async def test_send_action_missing_subject(self, mock_mail_root):
        """Test send action fails when subject is missing."""
        request = MailInput(
            action="send",
            from_email="sender@example.com",
            to_email="recipient@example.com",
            body="Test body",
        )
        result = await mail(request)

        assert result.action == "send"
        assert result.error is not None
        assert "subject" in result.error.lower()

    @pytest.mark.asyncio
    async def test_send_action_missing_body(self, mock_mail_root):
        """Test send action fails when body is missing."""
        request = MailInput(
            action="send",
            from_email="sender@example.com",
            to_email="recipient@example.com",
            subject="Test Subject",
        )
        result = await mail(request)

        assert result.action == "send"
        assert result.error is not None
        assert "body" in result.error.lower()

    @pytest.mark.asyncio
    async def test_send_action_with_multiple_recipients(self, mock_mail_root):
        """Test sending to multiple recipients via meta-tool."""
        request = MailInput(
            action="send",
            from_email="sender@example.com",
            to_email=["r1@example.com", "r2@example.com"],
            subject="Team Update",
            body="Hello team!",
            cc="manager@example.com",
        )
        result = await mail(request)

        assert result.action == "send"
        assert result.error is None
        assert result.send is not None
        assert result.send.success is True

    @pytest.mark.asyncio
    async def test_send_action_html_format(self, mock_mail_root):
        """Test sending HTML email via meta-tool."""
        request = MailInput(
            action="send",
            from_email="sender@example.com",
            to_email="recipient@example.com",
            subject="HTML Email",
            body="<html><body><h1>Hello!</h1></body></html>",
            body_format="html",
        )
        result = await mail(request)

        assert result.action == "send"
        assert result.error is None
        assert result.send is not None
        assert result.send.success is True

    @pytest.mark.asyncio
    async def test_read_action_success(self, mock_mail_root):
        """Test reading a mail via meta-tool."""
        # First send a mail
        send_request = MailInput(
            action="send",
            from_email="sender@example.com",
            to_email="recipient@example.com",
            subject="Mail to Read",
            body="Content to read",
        )
        send_result = await mail(send_request)
        assert send_result.send is not None
        mail_id = send_result.send.mail_id

        # Then read it
        read_request = MailInput(action="read", mail_id=mail_id)
        result = await mail(read_request)

        assert result.action == "read"
        assert result.error is None
        assert result.read is not None
        assert result.read.mail["mail_id"] == mail_id

    @pytest.mark.asyncio
    async def test_read_action_missing_mail_id(self, mock_mail_root):
        """Test read action fails when mail_id is missing."""
        request = MailInput(action="read")
        result = await mail(request)

        assert result.action == "read"
        assert result.error is not None
        assert "mail_id" in result.error.lower()

    @pytest.mark.asyncio
    async def test_read_action_nonexistent_mail(self, mock_mail_root):
        """Test reading a non-existent mail."""
        request = MailInput(action="read", mail_id="<nonexistent@example.com>")
        result = await mail(request)

        assert result.action == "read"
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_list_action_empty_mailbox(self, mock_mail_root):
        """Test listing mails on empty mailbox."""
        request = MailInput(action="list")
        result = await mail(request)

        assert result.action == "list"
        assert result.error is None
        assert result.list is not None
        assert result.list.mails == []
        assert result.list.count == 0
        assert result.list.has_more is False

    @pytest.mark.asyncio
    async def test_list_action_with_mails(self, mock_mail_root):
        """Test listing mails."""
        # Send some mails
        for i in range(3):
            send_request = MailInput(
                action="send",
                from_email="sender@example.com",
                to_email="recipient@example.com",
                subject=f"Mail {i}",
                body=f"Body {i}",
            )
            await mail(send_request)

        # List them
        request = MailInput(action="list")
        result = await mail(request)

        assert result.action == "list"
        assert result.error is None
        assert result.list is not None
        assert result.list.count == 3
        assert len(result.list.mails) == 3

    @pytest.mark.asyncio
    async def test_list_action_with_pagination(self, mock_mail_root):
        """Test listing mails with pagination."""
        # Send 5 mails
        for i in range(5):
            send_request = MailInput(
                action="send",
                from_email="sender@example.com",
                to_email="recipient@example.com",
                subject=f"Mail {i}",
                body=f"Body {i}",
            )
            await mail(send_request)

        # List with limit
        request = MailInput(action="list", page=0, limit=2)
        result = await mail(request)

        assert result.action == "list"
        assert result.error is None
        assert result.list is not None
        assert result.list.count == 2
        assert result.list.page == 0
        assert result.list.limit == 2
        assert result.list.has_more is True

    @pytest.mark.asyncio
    async def test_search_action_empty_results(self, mock_mail_root):
        """Test search with no matching results."""
        request = MailInput(
            action="search",
            search_from="nonexistent@example.com",
        )
        result = await mail(request)

        assert result.action == "search"
        assert result.error is None
        assert result.search is not None
        assert result.search.mails == []
        assert result.search.count == 0

    @pytest.mark.asyncio
    async def test_search_action_by_sender(self, mock_mail_root):
        """Test searching mails by sender."""
        # Send mails from different senders
        await mail(
            MailInput(
                action="send",
                from_email="alice@example.com",
                to_email="recipient@example.com",
                subject="From Alice",
                body="Hello from Alice",
            )
        )
        await mail(
            MailInput(
                action="send",
                from_email="bob@example.com",
                to_email="recipient@example.com",
                subject="From Bob",
                body="Hello from Bob",
            )
        )

        # Search for Alice's mails
        request = MailInput(action="search", search_from="alice@example.com")
        result = await mail(request)

        assert result.action == "search"
        assert result.error is None
        assert result.search is not None
        assert result.search.count == 1
        assert result.search.mails[0].from_email == "alice@example.com"

    @pytest.mark.asyncio
    async def test_search_action_by_subject(self, mock_mail_root):
        """Test searching mails by subject."""
        await mail(
            MailInput(
                action="send",
                from_email="sender@example.com",
                to_email="recipient@example.com",
                subject="Important Meeting",
                body="Please attend",
            )
        )
        await mail(
            MailInput(
                action="send",
                from_email="sender@example.com",
                to_email="recipient@example.com",
                subject="Casual Chat",
                body="How are you?",
            )
        )

        # Search by subject
        request = MailInput(action="search", search_subject="Meeting")
        result = await mail(request)

        assert result.action == "search"
        assert result.error is None
        assert result.search is not None
        assert result.search.count == 1
        assert "Meeting" in result.search.mails[0].subject

    @pytest.mark.asyncio
    async def test_reply_action_success(self, mock_mail_root):
        """Test replying to a mail via meta-tool."""
        # First send a mail
        send_result = await mail(
            MailInput(
                action="send",
                from_email="sender@example.com",
                to_email="recipient@example.com",
                subject="Original Message",
                body="Original content",
            )
        )
        assert send_result.send is not None
        mail_id = send_result.send.mail_id

        # Reply to it
        reply_request = MailInput(
            action="reply",
            mail_id=mail_id,
            body="This is my reply",
        )
        result = await mail(reply_request)

        assert result.action == "reply"
        assert result.error is None
        assert result.reply is not None
        assert result.reply.success is True

    @pytest.mark.asyncio
    async def test_reply_action_missing_mail_id(self, mock_mail_root):
        """Test reply action fails when mail_id is missing."""
        request = MailInput(action="reply", body="Reply content")
        result = await mail(request)

        assert result.action == "reply"
        assert result.error is not None
        assert "mail_id" in result.error.lower()

    @pytest.mark.asyncio
    async def test_reply_action_missing_body(self, mock_mail_root):
        """Test reply action fails when body is missing."""
        request = MailInput(action="reply", mail_id="<some-id@example.com>")
        result = await mail(request)

        assert result.action == "reply"
        assert result.error is not None
        assert "body" in result.error.lower()

    @pytest.mark.asyncio
    async def test_reply_all_action_success(self, mock_mail_root):
        """Test reply-all via meta-tool."""
        # Send a mail with multiple recipients
        send_result = await mail(
            MailInput(
                action="send",
                from_email="sender@example.com",
                to_email=["r1@example.com", "r2@example.com"],
                subject="Group Discussion",
                body="Let's discuss",
                cc="observer@example.com",
            )
        )
        assert send_result.send is not None
        mail_id = send_result.send.mail_id

        # Reply all
        reply_all_request = MailInput(
            action="reply_all",
            mail_id=mail_id,
            body="My reply to all",
        )
        result = await mail(reply_all_request)

        assert result.action == "reply_all"
        assert result.error is None
        assert result.reply_all is not None
        assert result.reply_all.success is True

    @pytest.mark.asyncio
    async def test_reply_all_action_missing_mail_id(self, mock_mail_root):
        """Test reply_all action fails when mail_id is missing."""
        request = MailInput(action="reply_all", body="Reply content")
        result = await mail(request)

        assert result.action == "reply_all"
        assert result.error is not None
        assert "mail_id" in result.error.lower()

    @pytest.mark.asyncio
    async def test_forward_action_success(self, mock_mail_root):
        """Test forwarding a mail via meta-tool."""
        # Send an original mail
        send_result = await mail(
            MailInput(
                action="send",
                from_email="sender@example.com",
                to_email="recipient@example.com",
                subject="Original for Forwarding",
                body="Content to forward",
            )
        )
        assert send_result.send is not None
        mail_id = send_result.send.mail_id

        # Forward it
        forward_request = MailInput(
            action="forward",
            mail_id=mail_id,
            to_email="newrecipient@example.com",
            body="FYI - see below",
        )
        result = await mail(forward_request)

        assert result.action == "forward"
        assert result.error is None
        assert result.forward is not None
        assert result.forward.success is True

    @pytest.mark.asyncio
    async def test_forward_action_missing_mail_id(self, mock_mail_root):
        """Test forward action fails when mail_id is missing."""
        request = MailInput(action="forward", to_email="recipient@example.com")
        result = await mail(request)

        assert result.action == "forward"
        assert result.error is not None
        assert "mail_id" in result.error.lower()

    @pytest.mark.asyncio
    async def test_forward_action_missing_to_email(self, mock_mail_root):
        """Test forward action fails when to_email is missing."""
        request = MailInput(action="forward", mail_id="<some-id@example.com>")
        result = await mail(request)

        assert result.action == "forward"
        assert result.error is not None
        assert "to_email" in result.error.lower()


class TestMailSchemaMetaTool:
    """Test the mail_schema meta-tool."""

    def test_schema_input(self):
        """Test getting input schema."""
        request = SchemaInput(model="input")
        result = mail_schema(request)

        assert result.model == "input"
        assert "properties" in result.json_schema
        assert "action" in result.json_schema["properties"]

    def test_schema_output(self):
        """Test getting output schema."""
        request = SchemaInput(model="output")
        result = mail_schema(request)

        assert result.model == "output"
        assert "properties" in result.json_schema
        assert "action" in result.json_schema["properties"]

    def test_schema_send_result(self):
        """Test getting SendResult schema."""
        request = SchemaInput(model="SendResult")
        result = mail_schema(request)

        assert result.model == "SendResult"
        assert "properties" in result.json_schema
        assert "success" in result.json_schema["properties"]
        assert "error" in result.json_schema["properties"]

    def test_schema_mail_list_result(self):
        """Test getting MailListResult schema."""
        request = SchemaInput(model="MailListResult")
        result = mail_schema(request)

        assert result.model == "MailListResult"
        assert "properties" in result.json_schema
        assert "mails" in result.json_schema["properties"]
        assert "count" in result.json_schema["properties"]
        assert "has_more" in result.json_schema["properties"]

    def test_schema_unknown_model(self):
        """Test getting schema for unknown model."""
        request = SchemaInput(model="UnknownModel")
        result = mail_schema(request)

        assert result.model == "UnknownModel"
        assert "error" in result.json_schema


class TestSendResultModel:
    """Test SendResult model includes all fields."""

    def test_send_result_has_error_field(self):
        """Test that SendResult includes the error field."""
        result = SendResult(
            success=False,
            mail_id=None,
            recipients_count=None,
            message="Validation failed",
            error="Invalid email address",
        )
        assert result.error == "Invalid email address"

    def test_send_result_error_optional(self):
        """Test that error field is optional."""
        result = SendResult(
            success=True,
            mail_id="<test@example.com>",
            recipients_count=3,
            message="Mail sent successfully",
        )
        assert result.error is None


class TestMailListResultModel:
    """Test MailListResult model has correct fields."""

    def test_mail_list_result_has_count_and_has_more(self):
        """Test that MailListResult has count and has_more fields."""
        result = MailListResult(
            mails=[],
            count=0,
            page=0,
            limit=50,
            has_more=False,
        )
        assert result.count == 0
        assert result.has_more is False

    def test_mail_list_result_with_mails(self):
        """Test MailListResult with mails."""
        result = MailListResult(
            mails=[
                MailSummaryItem(
                    mail_id="<1@example.com>",
                    timestamp="2024-01-01T00:00:00",
                    from_email="sender@example.com",
                    to=["recipient@example.com"],
                    subject="Test",
                ),
                MailSummaryItem(
                    mail_id="<2@example.com>",
                    timestamp="2024-01-02T00:00:00",
                    from_email="sender@example.com",
                    to=["recipient@example.com"],
                    subject="Test 2",
                ),
            ],
            count=2,
            page=0,
            limit=50,
            has_more=False,
        )
        assert result.count == 2
        assert len(result.mails) == 2
