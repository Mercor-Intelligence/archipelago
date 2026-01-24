import mailbox
import os
import re
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Annotated, Literal

from models.mail import MailResponse
from pydantic import Field
from utils.config import MAX_SUBJECT_LENGTH
from utils.decorators import make_async_background
from utils.path import get_mbox_path, resolve_mail_path

# Email validation pattern
EMAIL_PATTERN = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")


def _is_valid_email(email: str) -> bool:
    """Validate email address format."""
    return bool(EMAIL_PATTERN.match(email))


def _validate_email_list(emails: list[str], field_name: str) -> str | None:
    """Validate a list of email addresses. Returns error message or None."""
    for email in emails:
        if not _is_valid_email(email):
            return f"{field_name}: Invalid email address: {email}"
    return None


@make_async_background
def send_mail(
    from_email: Annotated[str, Field(description="Sender email address")],
    to_email: Annotated[
        str | list[str], Field(description="Recipient email address(es)")
    ],
    subject: Annotated[str, Field(description="Email subject line")],
    body: Annotated[str, Field(description="Email body content")],
    cc: Annotated[str | list[str] | None, Field(description="CC recipient(s)")] = None,
    bcc: Annotated[
        str | list[str] | None, Field(description="BCC recipient(s)")
    ] = None,
    attachments: Annotated[
        list[str] | None, Field(description="File paths to attach")
    ] = None,
    body_format: Annotated[
        Literal["plain", "html"], Field(description="Body format")
    ] = "plain",
    thread_id: Annotated[str | None, Field(description="Thread identifier")] = None,
    in_reply_to: Annotated[
        str | None, Field(description="Message-ID being replied to")
    ] = None,
    references: Annotated[
        list[str] | None, Field(description="List of referenced Message-IDs")
    ] = None,
) -> str:
    """Send an email."""
    # Validate from_email
    if not _is_valid_email(from_email):
        response = MailResponse(
            success=False,
            mail_id=None,
            recipients_count=None,
            message="Validation failed",
            error="from_email: Invalid email address",
        )
        return str(response)

    # Normalize to_email to list
    to_list = [to_email] if isinstance(to_email, str) else to_email

    # Validate to_email is not empty
    if not to_list:
        response = MailResponse(
            success=False,
            mail_id=None,
            recipients_count=None,
            message="Validation failed",
            error="to_email: At least one recipient is required",
        )
        return str(response)

    # Validate to_email addresses
    to_error = _validate_email_list(to_list, "to_email")
    if to_error:
        response = MailResponse(
            success=False,
            mail_id=None,
            recipients_count=None,
            message="Validation failed",
            error=to_error,
        )
        return str(response)

    # Validate subject
    if not subject or not subject.strip():
        response = MailResponse(
            success=False,
            mail_id=None,
            recipients_count=None,
            message="Validation failed",
            error="subject: Subject cannot be empty",
        )
        return str(response)

    if len(subject) > MAX_SUBJECT_LENGTH:
        response = MailResponse(
            success=False,
            mail_id=None,
            recipients_count=None,
            message="Validation failed",
            error=f"subject: Subject exceeds maximum length of {MAX_SUBJECT_LENGTH}",
        )
        return str(response)

    cc_list_normalized = [cc] if isinstance(cc, str) else (cc or [])
    bcc_list_normalized = [bcc] if isinstance(bcc, str) else (bcc or [])

    # Validate CC addresses
    if cc_list_normalized:
        cc_error = _validate_email_list(cc_list_normalized, "cc")
        if cc_error:
            response = MailResponse(
                success=False,
                mail_id=None,
                recipients_count=None,
                message="Validation failed",
                error=cc_error,
            )
            return str(response)

    # Validate BCC addresses
    if bcc_list_normalized:
        bcc_error = _validate_email_list(bcc_list_normalized, "bcc")
        if bcc_error:
            response = MailResponse(
                success=False,
                mail_id=None,
                recipients_count=None,
                message="Validation failed",
                error=bcc_error,
            )
            return str(response)

    timestamp = datetime.now()
    attachment_list = attachments or []

    # Create an EmailMessage
    msg = EmailMessage()
    msg["From"] = from_email
    msg["To"] = ", ".join(to_list)
    msg["Subject"] = subject
    msg["Date"] = formatdate(timestamp.timestamp(), localtime=True)
    msg["Message-ID"] = make_msgid(domain=from_email.split("@")[-1])

    mail_id = msg["Message-ID"]

    # Set threading headers if provided
    if thread_id:
        msg["X-Thread-ID"] = thread_id
    elif not in_reply_to:
        # If no thread_id and not a reply, this message starts its own thread
        msg["X-Thread-ID"] = mail_id

    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        # Update references list
        if not references:
            references = [in_reply_to]
        elif in_reply_to not in references:
            references = references + [in_reply_to]

    if references:
        msg["References"] = " ".join(references)

    # Add custom headers for metadata
    msg["X-Body-Format"] = body_format

    if cc_list_normalized:
        msg["Cc"] = ", ".join(cc_list_normalized)
    if bcc_list_normalized:
        msg["Bcc"] = ", ".join(bcc_list_normalized)
    if attachment_list:
        msg["X-Attachments"] = ", ".join(attachment_list)

    # Set the body content
    if body_format == "html":
        msg.set_content(body, subtype="html")
    else:
        msg.set_content(body)

    # Ensure mail directory exists
    mail_dir = resolve_mail_path("")
    os.makedirs(mail_dir, exist_ok=True)

    # Append to mbox file
    mbox_path = get_mbox_path()
    try:
        mbox = mailbox.mbox(mbox_path)
        try:
            mbox.lock()
        except (BlockingIOError, OSError) as lock_error:
            response = MailResponse(
                success=False,
                mail_id=None,
                recipients_count=None,
                message="Mailbox is currently busy. Please try again in a moment.",
                error=repr(lock_error),
            )
            return str(response)

        try:
            mbox.add(msg)
            mbox.flush()
        finally:
            mbox.unlock()
            mbox.close()
    except Exception as exc:
        response = MailResponse(
            success=False,
            mail_id=None,
            recipients_count=None,
            message="Failed to save mail",
            error=repr(exc),
        )
        return str(response)

    recipients_count = len(to_list) + len(cc_list_normalized) + len(bcc_list_normalized)

    response = MailResponse(
        success=True,
        mail_id=mail_id,
        recipients_count=recipients_count,
        message="Mail sent successfully",
        error=None,
    )
    return str(response)
