import mailbox
import os
from email.header import Header, decode_header
from email.utils import parseaddr

# Module-level linesep, same as mailbox module uses internally
_linesep = os.linesep.encode("ascii")


class UTF8Mbox(mailbox.mbox):
    """mbox subclass that handles non-ASCII characters in From_ separator lines.

    Python's mailbox.mbox hardcodes ``decode('ascii')`` for the ``From ``
    separator line, which raises ``UnicodeDecodeError`` for email addresses
    with accented characters (e.g. amélie.petrov@, chloé.mbeki@).  This
    subclass decodes that line as UTF-8 instead.
    """

    def get_message(self, key):
        start, stop = self._lookup(key)
        self._file.seek(start)
        from_line = (
            self._file.readline()
            .replace(_linesep, b"")
            .decode("utf-8", errors="replace")
        )
        string = self._file.read(stop - self._file.tell())
        msg = self._message_factory(string.replace(_linesep, b"\n"))
        msg.set_unixfrom(from_line)
        msg.set_from(from_line[5:])
        return msg


def safe_get_header(message, name: str, default: str = "") -> str:
    """Safely extract a header value as a plain string.

    Python's compat32 email policy returns ``email.header.Header`` objects
    for headers that contain raw non-ASCII bytes (e.g. em dashes in Subject
    lines).  Calling ``str()`` on such objects replaces non-ASCII characters
    with U+FFFD.  This helper decodes the underlying bytes as UTF-8 instead.
    """
    value = message[name]
    if value is None:
        return default
    if isinstance(value, str):
        return value
    if isinstance(value, Header):
        parts = decode_header(value)
        result: list[str] = []
        for data, charset in parts:
            if isinstance(data, bytes):
                result.append(
                    data.decode(
                        charset if charset and charset != "unknown-8bit" else "utf-8",
                        errors="replace",
                    )
                )
            else:
                result.append(data)
        return "".join(result)
    return str(value)


def parse_email_list(email_str) -> list[str]:
    """Parse a comma-separated email string into a list of email addresses.

    Accepts ``str`` or ``email.header.Header`` objects (which can appear when
    headers contain non-ASCII characters like accented names).
    """
    if not email_str:
        return []
    if isinstance(email_str, Header):
        parts = decode_header(email_str)
        segments: list[str] = []
        for data, charset in parts:
            if isinstance(data, bytes):
                segments.append(
                    data.decode(
                        charset if charset and charset != "unknown-8bit" else "utf-8",
                        errors="replace",
                    )
                )
            else:
                segments.append(data)
        email_str = "".join(segments)
    emails = []
    for part in email_str.split(","):
        _, email = parseaddr(part.strip())
        if email:
            emails.append(email)
    return emails


def parse_message_to_dict(message) -> dict:
    """Parse an email message object to a dictionary compatible with MailData model."""
    # Extract recipients from headers
    to_list = parse_email_list(message.get("To", ""))
    cc_list = parse_email_list(message.get("Cc", "")) or None
    bcc_list = parse_email_list(message.get("Bcc", "")) or None

    # Extract attachments from custom header
    attachments_str = message.get("X-Attachments", "")
    attachments = (
        [a.strip() for a in attachments_str.split(",") if a.strip()]
        if attachments_str
        else None
    )

    # Get body content
    body = ""
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload is not None:
                    body = payload.decode("utf-8", errors="ignore")
                    break
            elif part.get_content_type() == "text/html" and not body:
                payload = part.get_payload(decode=True)
                if payload is not None:
                    body = payload.decode("utf-8", errors="ignore")
    else:
        body = message.get_payload(decode=True)
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="ignore")
        elif body is None:
            body = ""

    # Extract timestamp from Date header
    date_str = message.get("Date", "")

    # Extract threading information
    thread_id = message.get("X-Thread-ID", None)
    in_reply_to = message.get("In-Reply-To", None)
    references_str = message.get("References", "")
    references = references_str.split() if references_str else None

    return {
        "mail_id": message.get("Message-ID", ""),
        "timestamp": date_str,
        "from": parseaddr(safe_get_header(message, "From"))[1],
        "to": to_list,
        "subject": safe_get_header(message, "Subject"),
        "body": body,
        "body_format": message.get("X-Body-Format", "plain"),
        "cc": cc_list,
        "bcc": bcc_list,
        "attachments": attachments,
        "thread_id": thread_id,
        "in_reply_to": in_reply_to,
        "references": references,
    }
