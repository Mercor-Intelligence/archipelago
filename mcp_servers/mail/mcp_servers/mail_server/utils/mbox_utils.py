import mailbox
import os
import re
from datetime import UTC, datetime
from email.errors import HeaderParseError
from email.header import Header, decode_header
from email.utils import parseaddr

_linesep = os.linesep.encode("ascii")


class UTF8Mbox(mailbox.mbox):
    """mbox subclass that handles non-ASCII characters in From_ separator lines."""

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


_FOLD = re.compile(r"[ \t]*\r?\n[ \t]+")
_ADDR_SPEC = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _unfold(value: str) -> str:
    """Undo RFC 5322 header folding.

    A CRLF followed by whitespace is line-wrapping, not content. Left in, it
    splits a subject mid-word for search and makes the value illegal to set as
    a header on a reply or forward. Whitespace on either side of the break
    collapses into the one space that stands for it, so a value folded at a
    point that already had a space does not come back with two.
    """
    return _FOLD.sub(" ", value).strip()


def _decode_encoded_words(value: str | Header) -> str:
    """Decode RFC 2047 encoded-words to the text a person would read."""
    result: list[str] = []
    for data, charset in decode_header(value):
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


def _normalize_header_text(value: str) -> str:
    """Undo the transport encoding of one raw header value."""
    # Unfold before decoding: decode_header() strips the leading whitespace of
    # each continuation line, so decoding first loses the space that separated
    # the text either side of the break. Adjacent encoded-words still join
    # without a space, which is what RFC 2047 requires.
    unfolded = _unfold(value)
    try:
        return _decode_encoded_words(unfolded).strip()
    except (ValueError, LookupError, UnicodeDecodeError, HeaderParseError):
        return unfolded


def safe_get_header(message, name: str, default: str = "") -> str:
    """Extract a header as the plain text a person would read.

    Seeded messages carry headers in their on-the-wire form, so the value can
    be folded across lines and non-ASCII text encoded as RFC 2047
    encoded-words. Both are transport encoding rather than content, and are
    undone here so every caller sees one readable line. Every header this
    server lifts into a response or copies onto an outgoing message goes
    through here: a value left folded is illegal to set as a header and aborts
    the reply or forward that carries it.
    """
    value = message[name]
    if value is None:
        return default
    if isinstance(value, Header):
        # A Header instance holds already-decoded chunks and is never folded.
        try:
            return _decode_encoded_words(value).strip()
        except (ValueError, LookupError, UnicodeDecodeError, HeaderParseError):
            return str(value)
    if not isinstance(value, str):
        return _unfold(str(value))
    return _normalize_header_text(value)


def optional_header(message, name: str) -> str | None:
    """Normalized header value, or None when the header is absent or empty."""
    value = safe_get_header(message, name)
    return value or None


def _split_on_separators(value: str) -> list[str]:
    """Split an address or attachment list on its separators.

    RFC 5322 separates addresses with commas, but Outlook and Exchange emit
    semicolons and seeded mail carries both. Separators inside a quoted display
    name or an angle-addr are content, so only top-level ones split.
    """
    parts: list[str] = []
    buffer: list[str] = []
    quoted = False
    angled = False
    for char in value:
        if char == '"' and not angled:
            quoted = not quoted
        elif char == "<" and not quoted:
            angled = True
        elif char == ">" and not quoted:
            angled = False
        elif char in ",;" and not quoted and not angled:
            parts.append("".join(buffer))
            buffer = []
            continue
        buffer.append(char)
    parts.append("".join(buffer))
    return [part.strip() for part in parts if part.strip()]


def as_utc(value: datetime | None) -> datetime:
    """Normalize naive/aware datetimes to UTC for safe comparisons/sorts."""
    if value is None:
        return datetime(1970, 1, 1, tzinfo=UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_email_list(email_str) -> list[str]:
    """Parse an address header into the list of addresses it names.

    Seeded headers are on-the-wire values, so they may be folded, carry RFC 2047
    encoded-words in display names, and use semicolons as separators. They also
    routinely carry an unquoted comma inside a display name ("Sarah Kim, Esq.
    <skim@x>"), which splits into a fragment that is not an address at all; such
    fragments are dropped rather than reported as recipients.
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
    else:
        email_str = _normalize_header_text(str(email_str))
    emails = []
    for part in _split_on_separators(email_str):
        emails.append(extract_address(part))
    return [email for email in emails if email]


def extract_address(value: str) -> str:
    """Return the addr-spec in one address, or "" when it names none.

    ``parseaddr`` puts the addr-spec in the second slot, but a header written as
    ``sender@example.com <>`` has an empty angle-addr and defeats it entirely,
    returning neither name nor address. Every mail client still shows that
    message as coming from someone, so the addr-spec is recovered from the text
    rather than the message being listed as having no sender.
    """
    name, address = parseaddr(value)
    if "@" in address:
        return address
    if "@" in name:
        return parseaddr(name)[1] or name.strip()
    match = _ADDR_SPEC.search(value)
    return match.group(0) if match else ""


def parse_attachment_list(value) -> list[str]:
    """Parse the X-Attachments header into individual file names.

    Written by this server as a comma-separated list, but seeded mail also uses
    semicolons and folds the header across lines. A folded value carries a
    newline, which is illegal in the header a reply or forward would then write.
    """
    if not value:
        return []
    text = _normalize_header_text(str(value))
    return _split_on_separators(text)


def message_identity(message) -> str:
    """A normalized fingerprint of who/when/what, used to tell copies apart.

    Two sources holding the same message disagree on presentation - one quotes
    the display name, the other repeats the address in it - so the fingerprint
    is built from the parsed addresses rather than the raw header text. Two
    messages that merely reuse a Message-ID differ here.
    """
    parts = [
        extract_address(safe_get_header(message, "From")),
        ",".join(sorted(parse_email_list(message.get("To", "")))),
        ",".join(sorted(parse_email_list(message.get("Cc", "")))),
        safe_get_header(message, "Subject"),
        safe_get_header(message, "Date"),
    ]
    return "\x1f".join(parts)


def parse_message_to_dict(message) -> dict:
    """Parse an email message object to a dictionary compatible with MailData model."""
    # Extract recipients from headers
    to_list = parse_email_list(message.get("To", ""))
    cc_list = parse_email_list(message.get("Cc", "")) or None
    bcc_list = parse_email_list(message.get("Bcc", "")) or None

    # Extract attachments from custom header
    attachments = parse_attachment_list(message.get("X-Attachments", "")) or None

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
    date_str = safe_get_header(message, "Date")

    # Extract threading information
    thread_id = optional_header(message, "X-Thread-ID")
    in_reply_to = optional_header(message, "In-Reply-To")
    references_str = safe_get_header(message, "References")
    references = references_str.split() if references_str else None

    return {
        "mail_id": safe_get_header(message, "Message-ID"),
        "timestamp": date_str,
        "from": extract_address(safe_get_header(message, "From")),
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
