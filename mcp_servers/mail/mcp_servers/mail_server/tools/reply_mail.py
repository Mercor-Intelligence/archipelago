import mailbox
import os
from typing import Annotated, Literal

from models.mail import MailData
from pydantic import Field
from tools.send_mail import send_mail
from utils.mbox_utils import parse_message_to_dict
from utils.path import get_mbox_path


async def reply_mail(
    original_mail_id: Annotated[
        str, Field(description="Message-ID of the email to reply to")
    ],
    body: Annotated[str, Field(description="Reply message body")],
    attachments: Annotated[
        list[str] | None, Field(description="File paths to attach")
    ] = None,
    body_format: Annotated[
        Literal["plain", "html"], Field(description="Body format")
    ] = "plain",
) -> str:
    """Reply to an email, preserving the thread and addressing only the sender."""
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

    reply_to = original_mail.from_email
    reply_from = original_mail.to[0] if original_mail.to else "user@example.com"

    subject = original_mail.subject
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    thread_id = original_mail.thread_id or original_mail.mail_id

    references = original_mail.references or []
    if original_mail.mail_id not in references:
        references = references + [original_mail.mail_id]

    return await send_mail(
        from_email=reply_from,
        to_email=reply_to,
        subject=subject,
        body=body,
        attachments=attachments,
        body_format=body_format,
        thread_id=thread_id,
        in_reply_to=original_mail.mail_id,
        references=references,
    )
