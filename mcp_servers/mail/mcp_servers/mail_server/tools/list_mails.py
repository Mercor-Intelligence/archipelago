import logging
from email.utils import parsedate_to_datetime
from typing import Annotated

from models.mail import MailListResponse, MailSummary
from pydantic import Field
from utils.config import DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT
from utils.decorators import make_async_background
from utils.mail_store import load_mailbox
from utils.mbox_utils import (
    as_utc,
    extract_address,
    optional_header,
    parse_email_list,
    safe_get_header,
)

logger = logging.getLogger(__name__)


@make_async_background
def list_mails(
    limit: Annotated[
        int,
        Field(
            description="Maximum number of emails to return per request. Range: 1-100. Default: 50. Results are sorted by timestamp, most recent first.",
            ge=1,
            le=100,
        ),
    ] = 50,
    offset: Annotated[
        int,
        Field(
            description="Number of emails to skip for pagination. Default: 0. For example, offset=50 with limit=50 returns emails 51-100. Use with limit to paginate through results.",
            ge=0,
        ),
    ] = 0,
) -> str:
    """List emails with limit and offset (pagination). Use to browse the mailbox."""
    # Normalize limit to valid range
    if limit < 1:
        limit = DEFAULT_LIST_LIMIT
    if limit > MAX_LIST_LIMIT:
        limit = MAX_LIST_LIMIT

    # Normalize offset to non-negative
    if offset < 0:
        offset = 0

    try:
        load = load_mailbox()

        # Collect all messages with their timestamps for sorting
        messages_with_time = []
        for loaded in load.mails:
            try:
                date_str = safe_get_header(loaded.message, "Date")
                # Parse the date for sorting
                try:
                    timestamp = parsedate_to_datetime(date_str)
                except Exception:
                    # If parsing fails, use epoch time
                    timestamp = None

                messages_with_time.append(
                    (
                        loaded,
                        as_utc(timestamp) if timestamp is not None else None,
                        date_str,
                    )
                )
            except Exception as e:
                logger.warning(f"Skipping unparseable message: {e}")
                continue

        # Sort by timestamp (most recent first), handling None values
        messages_with_time.sort(
            key=lambda x: x[1]
            if x[1] is not None
            else parsedate_to_datetime("Thu, 1 Jan 1970 00:00:00 +0000"),
            reverse=True,
        )

        # Apply pagination
        paginated_messages = messages_with_time[offset : offset + limit]

        # Create summaries
        mail_summaries = []
        for loaded, _, date_str in paginated_messages:
            message = loaded.message
            try:
                to_list = parse_email_list(message.get("To", ""))
                summary = MailSummary.model_validate(
                    {
                        "mail_id": loaded.mail_id,
                        "timestamp": date_str,
                        "from": extract_address(safe_get_header(message, "From")),
                        "to": to_list,
                        "subject": safe_get_header(message, "Subject"),
                        "thread_id": optional_header(message, "X-Thread-ID"),
                        "in_reply_to": optional_header(message, "In-Reply-To"),
                    }
                )
                mail_summaries.append(summary)
            except Exception as e:
                logger.warning(f"Skipping message that failed summary validation: {e}")
                continue

        response = MailListResponse(
            mails=mail_summaries,
            error=load.error_text if load.scan_failed else None,
            warning=load.warning_text,
        )
        return str(response)
    except Exception as e:
        response = MailListResponse(mails=[], error=repr(e))
        return str(response)
