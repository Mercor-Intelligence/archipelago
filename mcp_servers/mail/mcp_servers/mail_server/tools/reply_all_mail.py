from models.mail import MailData, ReplyMailInput, SendMailInput
from tools._outgoing import outgoing_sender, reply_target, usable_addresses
from tools.send_mail import send_mail
from utils.mail_store import find_mail
from utils.mbox_utils import parse_message_to_dict


async def reply_all_mail(input: ReplyMailInput) -> str:
    """Reply to all recipients of an email. Use for reply-all."""
    original_mail_id = input.original_mail_id
    body = input.body
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

    reply_to, refusal = reply_target(original_mail.from_email, original_mail.mail_id)
    if reply_to is None:
        return refusal or "Error: cannot reply to this message"
    reply_from = outgoing_sender(original_mail.to)

    cc_list = []

    for recipient in usable_addresses(original_mail.to):
        if recipient != reply_from:
            cc_list.append(recipient)

    for cc_recipient in usable_addresses(original_mail.cc):
        if cc_recipient not in cc_list:
            cc_list.append(cc_recipient)

    subject = original_mail.subject
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    thread_id = original_mail.thread_id or original_mail.mail_id

    references = original_mail.references or []
    if original_mail.mail_id not in references:
        references = references + [original_mail.mail_id]

    return await send_mail(
        SendMailInput(
            from_email=reply_from,
            to_email=reply_to,
            subject=subject,
            body=body,
            cc=cc_list if cc_list else None,
            attachments=attachments,
            body_format=body_format,
            thread_id=thread_id,
            in_reply_to=original_mail.mail_id,
            references=references,
        )
    )
