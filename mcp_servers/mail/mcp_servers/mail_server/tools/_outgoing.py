"""Deriving an outgoing message's addresses from a message that was read.

A reply, a reply-all and a forward each build their addresses out of whatever
the original message's headers happened to hold. Seeded mail is real mail, so
those headers include shapes an address validator rejects. Left unchecked the
rejection surfaces as a pydantic ValidationError raised out of the tool rather
than as an answer the caller can act on, so the checks live here.
"""

from models.mail import MailResponse
from tools.send_mail import _is_valid_email
from utils.structured import StructuredText, structured

DEFAULT_SENDER = "user@example.com"


def usable_addresses(addresses: list[str] | None) -> list[str]:
    """Keep the addresses an outgoing message may legally carry."""
    return [address for address in addresses or [] if _is_valid_email(address)]


def outgoing_sender(recipients: list[str] | None) -> str:
    """Who a reply or forward is sent as: the original's first real recipient."""
    usable = usable_addresses(recipients)
    return usable[0] if usable else DEFAULT_SENDER


def reply_target(original_sender: str, mail_id: str) -> tuple[str | None, str | None]:
    """The address to reply to, or an explanation of why there is none."""
    if _is_valid_email(original_sender):
        return original_sender, None
    if original_sender:
        return None, (
            f"cannot reply to {mail_id} - its From header names "
            f"'{original_sender}', which is not a usable email address. "
            "Use send_mail with an explicit recipient instead."
        )
    return None, (
        f"cannot reply to {mail_id} - it carries no sender address in "
        "its From header. Use send_mail with an explicit recipient instead."
    )


def reply_refused(reason: str | None) -> StructuredText:
    """A reply that ``reply_target`` refused, as ``Error: <reason>``."""
    return structured(
        MailResponse(
            success=False,
            mail_id=None,
            recipients_count=None,
            message="Cannot reply to this message",
            error=reason or "cannot reply to this message",
        )
    )


def original_not_found(mail_id: str) -> StructuredText:
    """No mail with the given id, as ``Error: Original mail not found ...``."""
    return structured(
        MailResponse(
            success=False,
            mail_id=None,
            recipients_count=None,
            message="Original mail not found",
            error=f"Original mail not found with ID: {mail_id}",
        )
    )


def original_unreadable(reason: str) -> StructuredText:
    """The original mail could not be read, as ``Error reading original mail: ...``."""
    return StructuredText(
        f"Error reading original mail: {reason}",
        MailResponse(
            success=False,
            mail_id=None,
            recipients_count=None,
            message="Error reading original mail",
            error=reason,
        ),
    )
