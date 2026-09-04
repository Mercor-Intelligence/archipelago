from typing import Annotated

from models.mail import MailData
from pydantic import Field, ValidationError
from utils.decorators import make_async_background
from utils.mail_store import find_mail
from utils.mbox_utils import parse_message_to_dict


@make_async_background
def read_mail(
    mail_id: Annotated[
        str,
        Field(
            description="The Message-ID of the email to read in RFC 5322 format (e.g., '<unique-id@domain.com>'). Obtain this value from list_mails or search_mail results. Cannot be empty."
        ),
    ],
) -> str:
    """Read a single email by Message-ID. Use to get full message content."""
    # Validate mail_id is not empty
    if not mail_id or not mail_id.strip():
        return "Error: Invalid mail_id - cannot be empty"

    try:
        loaded, load = find_mail(mail_id)

        if loaded is not None:
            mail_data_dict = parse_message_to_dict(loaded.message)
            mail_data_dict["mail_id"] = loaded.mail_id
            mail_data = MailData.model_validate(mail_data_dict)
            return str(mail_data)

        # A source that could not be read may well be the one holding this mail,
        # so say so instead of reporting a clean miss.
        if load.scan_failed:
            return f"Failed to read mail: {load.error_text}"

        # Likewise a message listed under a different id than the one it claims
        # - the miss is explained by the warning, not by the mail being absent.
        if load.warning_text:
            return (
                f"Mail not found with ID: {mail_id}. "
                f"Some mail could not be read as seeded: {load.warning_text}"
            )

        return f"Mail not found with ID: {mail_id}"
    except ValidationError as e:
        error_messages = "; ".join(
            [f"{err['loc'][0]}: {err['msg']}" for err in e.errors()]
        )
        return f"Mail data validation failed: {error_messages}"
    except Exception as e:
        return f"Failed to read mail: {repr(e)}"
