import logging
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from models.mail import MailData, MailListResponse, MailSummary, SearchMailInput
from utils.config import DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT
from utils.decorators import make_async_background
from utils.mail_store import load_mailbox
from utils.mbox_utils import as_utc, parse_message_to_dict

logger = logging.getLogger(__name__)


@make_async_background
def search_mail(input: SearchMailInput) -> str:
    """Search emails by from_email, to_email, subject (partial match), body (partial match), after_date, before_date, or thread_id; limit applies. Use to find specific messages."""
    from_email = input.from_email
    to_email = input.to_email
    subject = input.subject
    body = input.body
    after_date = input.after_date
    before_date = input.before_date
    thread_id = input.thread_id
    limit = input.limit

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
            if after_datetime.tzinfo is None:
                after_datetime = after_datetime.replace(tzinfo=UTC)
        except ValueError:
            return "Error: Invalid after_date format. Use YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS"

    if before_date:
        try:
            try:
                before_datetime = datetime.fromisoformat(before_date)
            except ValueError:
                before_datetime = datetime.fromisoformat(f"{before_date}T23:59:59")
            if before_datetime.tzinfo is None:
                before_datetime = before_datetime.replace(tzinfo=UTC)
        except ValueError:
            return "Error: Invalid before_date format. Use YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS"

    try:
        load = load_mailbox()

        matching_messages = []
        for loaded in load.mails:
            try:
                mail_data_dict = parse_message_to_dict(loaded.message)
                mail_data_dict["mail_id"] = loaded.mail_id
                mail = MailData.model_validate(mail_data_dict)

                if from_email:
                    if from_email.lower() not in mail.from_email.lower():
                        continue

                if to_email:
                    if not any(
                        to_email.lower() in recipient.lower() for recipient in mail.to
                    ):
                        continue

                if subject:
                    if subject.lower() not in mail.subject.lower():
                        continue

                if body:
                    if body.lower() not in mail.body.lower():
                        continue

                if after_datetime or before_datetime:
                    try:
                        mail_datetime = parsedate_to_datetime(mail.timestamp)
                    except Exception:
                        try:
                            mail_datetime = datetime.fromisoformat(mail.timestamp)
                        except Exception:
                            continue

                    mail_datetime = as_utc(mail_datetime)
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

                matching_messages.append(
                    (
                        mail_data_dict,
                        as_utc(timestamp) if timestamp is not None else None,
                    )
                )

            except Exception as e:
                logger.warning(f"Failed to parse message {loaded.mail_id}: {e}")
                continue

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

        response = MailListResponse(
            mails=mail_summaries,
            error=load.error_text if load.scan_failed else None,
            warning=load.warning_text,
        )
        return str(response)
    except Exception as e:
        response = MailListResponse(mails=[], error=repr(e))
        return str(response)
