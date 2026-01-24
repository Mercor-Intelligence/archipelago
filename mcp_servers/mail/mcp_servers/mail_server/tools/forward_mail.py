import mailbox
import os
from typing import Annotated, Literal

from models.mail import MailData
from pydantic import Field
from tools.send_mail import send_mail
from utils.mbox_utils import parse_message_to_dict
from utils.path import get_mbox_path


async def forward_mail(
    original_mail_id: Annotated[
        str, Field(description="Message-ID of the email to forward")
    ],
    to_email: Annotated[str | list[str], Field(description="Recipient email(s)")],
    body: Annotated[str | None, Field(description="Additional message body")] = None,
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
) -> str:
    """Forward an email to new recipients."""
    mbox_path = get_mbox_path()

    if not os.path.exists(mbox_path):
        return f"Error: Original mail not found with ID: {original_mail_id}"

    try:
        mbox = mailbox.mbox(mbox_path)
        try:
            mbox.lock()
        except (BlockingIOError, OSError):
            return "Mailbox is currently busy. Please try again in a moment."

        try:
            original_mail = None
            for message in mbox:
                if message.get("Message-ID") == original_mail_id:
                    mail_data_dict = parse_message_to_dict(message)
                    original_mail = MailData.model_validate(mail_data_dict)
                    break

            if not original_mail:
                return f"Error: Original mail not found with ID: {original_mail_id}"
        finally:
            mbox.unlock()
            mbox.close()
    except Exception as e:
        return f"Error reading original mail: {repr(e)}"

    subject = original_mail.subject
    if not subject.lower().startswith("fwd:"):
        subject = f"Fwd: {subject}"

    forwarded_body_parts = []

    if body:
        forwarded_body_parts.append(body)
        forwarded_body_parts.append("")
        forwarded_body_parts.append("---------- Forwarded message ---------")
    else:
        forwarded_body_parts.append("---------- Forwarded message ---------")

    forwarded_body_parts.append(f"From: {original_mail.from_email}")
    forwarded_body_parts.append(f"Date: {original_mail.timestamp}")
    forwarded_body_parts.append(f"Subject: {original_mail.subject}")
    forwarded_body_parts.append(f"To: {', '.join(original_mail.to)}")

    if original_mail.cc:
        forwarded_body_parts.append(f"CC: {', '.join(original_mail.cc)}")

    forwarded_body_parts.append("")
    forwarded_body_parts.append(original_mail.body)

    forwarded_body = "\n".join(forwarded_body_parts)

    combined_attachments = []
    if original_mail.attachments:
        combined_attachments.extend(original_mail.attachments)
    if attachments:
        combined_attachments.extend(attachments)

    forward_from = original_mail.to[0] if original_mail.to else "user@example.com"

    return await send_mail(
        from_email=forward_from,
        to_email=to_email,
        subject=subject,
        body=forwarded_body,
        cc=cc,
        bcc=bcc,
        attachments=combined_attachments if combined_attachments else None,
        body_format=body_format,
    )
