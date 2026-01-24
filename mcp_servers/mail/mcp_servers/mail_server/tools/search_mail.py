import logging
import mailbox
import os
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Annotated

from models.mail import MailData, MailListResponse, MailSummary
from pydantic import Field
from utils.config import DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT
from utils.decorators import make_async_background
from utils.mbox_utils import parse_message_to_dict
from utils.path import get_mbox_path

logger = logging.getLogger(__name__)


@make_async_background
def search_mail(
    from_email: Annotated[
        str | None, Field(description="Filter by sender email")
    ] = None,
    to_email: Annotated[
        str | None, Field(description="Filter by recipient email")
    ] = None,
    subject: Annotated[
        str | None, Field(description="Filter by subject (partial match)")
    ] = None,
    after_date: Annotated[
        str | None, Field(description="Filter emails after this date (ISO format)")
    ] = None,
    before_date: Annotated[
        str | None, Field(description="Filter emails before this date (ISO format)")
    ] = None,
    thread_id: Annotated[str | None, Field(description="Filter by thread ID")] = None,
    limit: Annotated[
        int, Field(description="Maximum number of results", ge=1, le=100)
    ] = 50,
) -> str:
    """Search emails by sender, recipient, subject, date range, or thread."""

    if limit < 1:
        limit = DEFAULT_LIST_LIMIT
    if limit > MAX_LIST_LIMIT:
        limit = MAX_LIST_LIMIT

    after_datetime = None
    before_datetime = None

    if after_date:
        try:
            try:
                after_datetime = datetime.fromisoformat(after_date)
            except ValueError:
                after_datetime = datetime.fromisoformat(f"{after_date}T00:00:00")
        except ValueError:
            return "Error: Invalid after_date format. Use YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS"

    if before_date:
        try:
            try:
                before_datetime = datetime.fromisoformat(before_date)
            except ValueError:
                before_datetime = datetime.fromisoformat(f"{before_date}T23:59:59")
        except ValueError:
            return "Error: Invalid before_date format. Use YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS"

    mbox_path = get_mbox_path()

    if not os.path.exists(mbox_path):
        response = MailListResponse(mails=[], error=None)
        return str(response)

    try:
        mbox = mailbox.mbox(mbox_path)
        try:
            mbox.lock()
        except (BlockingIOError, OSError):
            response = MailListResponse(
                mails=[],
                error="Mailbox is currently busy. Please try again in a moment.",
            )
            return str(response)

        try:
            matching_messages = []
            for message in mbox:
                try:
                    mail_data_dict = parse_message_to_dict(message)
                    mail = MailData.model_validate(mail_data_dict)

                    if from_email:
                        if from_email.lower() not in mail.from_email.lower():
                            continue

                    if to_email:
                        if not any(
                            to_email.lower() in recipient.lower()
                            for recipient in mail.to
                        ):
                            continue

                    if subject:
                        if subject.lower() not in mail.subject.lower():
                            continue

                    if after_datetime or before_datetime:
                        try:
                            mail_datetime = parsedate_to_datetime(mail.timestamp)
                        except Exception:
                            try:
                                mail_datetime = datetime.fromisoformat(mail.timestamp)
                            except Exception:
                                continue

                        if after_datetime and mail_datetime < after_datetime:
                            continue
                        if before_datetime and mail_datetime > before_datetime:
                            continue

                    if thread_id:
                        if mail.thread_id != thread_id:
                            continue

                    try:
                        timestamp = parsedate_to_datetime(mail.timestamp)
                    except Exception:
                        try:
                            timestamp = datetime.fromisoformat(mail.timestamp)
                        except Exception:
                            timestamp = None

                    matching_messages.append((mail_data_dict, timestamp))

                except Exception as e:
                    logger.warning(f"Failed to parse message: {e}")
                    continue
        finally:
            mbox.unlock()
            mbox.close()

        matching_messages.sort(
            key=lambda x: x[1]
            if x[1] is not None
            else parsedate_to_datetime("Thu, 1 Jan 1970 00:00:00 +0000"),
            reverse=True,
        )

        mail_summaries = []
        for mail_data_dict, _ in matching_messages[:limit]:
            try:
                summary = MailSummary.model_validate(mail_data_dict)
                mail_summaries.append(summary)
            except Exception:
                continue

        response = MailListResponse(mails=mail_summaries, error=None)
        return str(response)
    except Exception as e:
        response = MailListResponse(mails=[], error=repr(e))
        return str(response)
