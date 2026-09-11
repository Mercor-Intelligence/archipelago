from models.mail import ForwardMailInput, MailData, SendMailInput
from tools._outgoing import outgoing_sender
from tools.send_mail import send_mail
from utils.mail_store import find_mail
from utils.mbox_utils import parse_message_to_dict


async def forward_mail(input: ForwardMailInput) -> str:
    """Forward an email to one or more addresses. Use to forward a message."""
    original_mail_id = input.original_mail_id
    to_email = input.to_email
    body = input.body
    cc = input.cc
    bcc = input.bcc
    attachments = input.attachments
    body_format = input.body_format

    try:
        loaded, load = find_mail(original_mail_id)

        if loaded is None:
            if load.error_text:
                return f"Error reading original mail: {load.error_text}"
            return f"Error: Original mail not found with ID: {original_mail_id}"

        mail_data_dict = parse_message_to_dict(loaded.message)
        mail_data_dict["mail_id"] = loaded.mail_id
        original_mail = MailData.model_validate(mail_data_dict)
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

    forward_from = outgoing_sender(original_mail.to)

    return await send_mail(
        SendMailInput(
            from_email=forward_from,
            to_email=to_email,
            subject=subject,
            body=forwarded_body,
            cc=cc,
            bcc=bcc,
            attachments=combined_attachments if combined_attachments else None,
            body_format=body_format,
        )
    )
