import re
from re import Pattern
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from utils.config import MAX_SUBJECT_LENGTH


class SendMailInput(BaseModel):
    """Input model for sending an email."""

    model_config = ConfigDict(extra="forbid")

    from_email: str = Field(
        ...,
        description="The sender's email address. Format and valid values depend on the specific use case.",
    )
    to_email: str | list[str] = Field(
        ...,
        description="The recipient's email address(es). Format and valid values depend on the specific use case.",
    )
    subject: str = Field(..., description="The email subject line")
    body: str = Field(..., description="The email body content")
    cc: str | list[str] | None = Field(None, description="Carbon copy recipients")
    bcc: str | list[str] | None = Field(
        None, description="Blind carbon copy recipients"
    )
    attachments: list[str] | None = Field(
        None, description="List of file paths to attach"
    )
    body_format: Literal["plain", "html"] = Field(
        default="plain", description="Format of the body - 'plain' or 'html'"
    )

    _EMAIL_PATTERN: ClassVar[Pattern[str]] = re.compile(
        r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    )

    @field_validator("from_email")
    @classmethod
    def _validate_from_email(cls, value: str) -> str:
        if not value or not cls._EMAIL_PATTERN.match(value):
            raise ValueError("Invalid from_email address")
        return value

    @field_validator("to_email")
    @classmethod
    def _validate_to_email(cls, value: str | list[str]) -> list[str]:
        """Normalize to list and validate all emails."""
        if isinstance(value, str):
            emails = [value]
        elif isinstance(value, list):
            emails = value
        else:
            raise ValueError("to_email must be a string or list of strings")

        if not emails:
            raise ValueError("to_email must contain at least one email address")

        for email in emails:
            if not isinstance(email, str) or not cls._EMAIL_PATTERN.match(email):
                raise ValueError(f"Invalid to_email address: {email}")

        return emails

    @field_validator("cc")
    @classmethod
    def _validate_cc(cls, value: str | list[str] | None) -> list[str] | None:
        """Normalize to list and validate all emails."""
        if value is None:
            return None

        if isinstance(value, str):
            emails = [value]
        elif isinstance(value, list):
            emails = value
        else:
            raise ValueError("cc must be a string or list of strings")

        for email in emails:
            if not isinstance(email, str) or not cls._EMAIL_PATTERN.match(email):
                raise ValueError(f"Invalid cc email address: {email}")

        return emails if emails else None

    @field_validator("bcc")
    @classmethod
    def _validate_bcc(cls, value: str | list[str] | None) -> list[str] | None:
        """Normalize to list and validate all emails."""
        if value is None:
            return None

        if isinstance(value, str):
            emails = [value]
        elif isinstance(value, list):
            emails = value
        else:
            raise ValueError("bcc must be a string or list of strings")

        for email in emails:
            if not isinstance(email, str) or not cls._EMAIL_PATTERN.match(email):
                raise ValueError(f"Invalid bcc email address: {email}")

        return emails if emails else None

    @field_validator("subject")
    @classmethod
    def _validate_subject(cls, value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("Subject must be a string")
        if not value.strip():
            raise ValueError("Subject cannot be empty")
        if len(value) > MAX_SUBJECT_LENGTH:
            raise ValueError(f"Subject must be {MAX_SUBJECT_LENGTH} characters or less")
        return value

    @field_validator("body")
    @classmethod
    def _validate_body(cls, value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("Body must be a string")
        return value

    @field_validator("attachments")
    @classmethod
    def _validate_attachments(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if not isinstance(value, list):
            raise ValueError("attachments must be a list")
        return value


class MailResponse(BaseModel):
    """Response model for mail sending operation."""

    model_config = ConfigDict(extra="forbid")

    success: bool = Field(..., description="Whether the mail was sent successfully")
    mail_id: str | None = Field(None, description="Unique identifier for the sent mail")
    recipients_count: int | None = Field(
        None, description="Total number of recipients (to + cc + bcc)"
    )
    message: str = Field(..., description="Human-readable status message")
    error: str | None = Field(None, description="Error message if sending failed")

    @field_validator("mail_id")
    @classmethod
    def _validate_mail_id(cls, value: str | None, info) -> str | None:
        """Ensure mail_id is present when success is True."""
        if info.data.get("success") and not value:
            raise ValueError("mail_id must be present when success is True")
        return value

    def __str__(self) -> str:
        """Format response for display."""
        if not self.success:
            return f"Error: {self.error or self.message}"
        return f"Mail sent successfully! Mail ID: {self.mail_id}, Recipients: {self.recipients_count}"


class MailData(BaseModel):
    """Model for the mail data stored in JSON files."""

    model_config = ConfigDict(extra="ignore")

    mail_id: str = Field(..., description="Unique identifier for the mail")
    timestamp: str = Field(
        ..., description="ISO format timestamp of when mail was sent"
    )
    from_email: str = Field(..., alias="from", description="Sender's email address")
    to: list[str] = Field(..., description="List of recipient email addresses")
    subject: str = Field(..., description="Email subject line")
    body: str = Field(..., description="Email body content")
    body_format: Literal["plain", "html"] = Field(..., description="Format of the body")
    cc: list[str] | None = Field(None, description="Carbon copy recipients")
    bcc: list[str] | None = Field(None, description="Blind carbon copy recipients")
    attachments: list[str] | None = Field(None, description="List of attachment paths")

    thread_id: str | None = Field(
        None, description="Thread ID for conversation grouping"
    )
    in_reply_to: str | None = Field(None, description="Message ID this is replying to")
    references: list[str] | None = Field(
        None, description="List of message IDs in thread"
    )

    def __str__(self) -> str:
        """Format mail data for display."""
        lines = [
            f"Mail ID: {self.mail_id}",
            f"Timestamp: {self.timestamp}",
            f"From: {self.from_email}",
            f"To: {', '.join(self.to)}",
        ]

        if self.cc:
            lines.append(f"CC: {', '.join(self.cc)}")
        if self.bcc:
            lines.append(f"BCC: {', '.join(self.bcc)}")

        lines.extend(
            [
                f"Subject: {self.subject}",
                f"Body Format: {self.body_format}",
            ]
        )

        if self.thread_id:
            lines.append(f"Thread ID: {self.thread_id}")
        if self.in_reply_to:
            lines.append(f"In Reply To: {self.in_reply_to}")

        lines.extend(["", "Body:", self.body])

        if self.attachments:
            lines.extend(
                [
                    "",
                    "Attachments:",
                ]
            )
            for att in self.attachments:
                lines.append(f"  - {att}")

        return "\n".join(lines)


class MailSummary(BaseModel):
    """Summary model for listing emails."""

    model_config = ConfigDict(extra="ignore")

    mail_id: str = Field(..., description="Unique identifier for the mail")
    timestamp: str = Field(..., description="ISO format timestamp")
    from_email: str = Field(..., alias="from", description="Sender's email address")
    to: list[str] = Field(..., description="List of recipient email addresses")
    subject: str = Field(..., description="Email subject line")
    thread_id: str | None = Field(
        None, description="Thread ID for conversation grouping"
    )
    in_reply_to: str | None = Field(None, description="Message ID this is replying to")

    def __str__(self) -> str:
        """Format mail summary for display."""
        lines = [
            f"Mail ID: {self.mail_id}",
            f"Timestamp: {self.timestamp}",
            f"From: {self.from_email}",
            f"To: {', '.join(self.to)} ({len(self.to)} recipient(s))",
            f"Subject: {self.subject}",
        ]
        if self.thread_id:
            lines.append(f"Thread: {self.thread_id}")
        if self.in_reply_to:
            lines.append(f"In Reply To: {self.in_reply_to}")
        return "\n".join(lines)


class MailListResponse(BaseModel):
    """Response model for listing emails."""

    model_config = ConfigDict(extra="forbid")

    mails: list[MailSummary] = Field(..., description="List of mail summaries")
    error: str | None = Field(None, description="Error message if listing failed")

    def __str__(self) -> str:
        """Format mail list for display."""
        if self.error:
            return f"Failed to list mails: {self.error}"

        if not self.mails:
            return "No emails found"

        lines = [f"Found {len(self.mails)} email(s):", ""]

        for idx, mail in enumerate(self.mails, 1):
            lines.append(f"{idx}. {mail}")
            lines.append("")

        return "\n".join(lines).strip()
