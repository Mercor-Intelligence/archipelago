"""Decode a MIME email (`.eml`) into readable text for `read_text_file`.

`.eml` is a text-based format, but its useful content is not its raw source:
headers arrive RFC 2047-encoded, bodies arrive quoted-printable or base64, and
an attachment-bearing message buries both under a base64 blob. Returning the
raw bytes would nominally "support" the extension while still forcing agents
out to the shell, so `read_text_file` renders a digest of the message instead.

Deliberately stdlib-only -- no loguru/pydantic/fastmcp -- so the rendering and
its refusal wording can be unit-tested offline without standing up the server.

Nothing here executes, renders, or writes any part of the message: HTML bodies
come back as source text, attachment payloads are only measured, and an
attached message is listed rather than spliced into the outer message's body.
"""

import email
from collections.abc import Iterator
from email.header import decode_header, make_header
from email.message import Message
from typing import cast

# Parsing decodes the whole message (including base64 parts) in memory, so the
# cap bounds the work a single tool call can trigger. 25 MB is the per-message
# ceiling common mail providers impose, so real email evidence lands under it.
EML_MAX_BYTES = 25 * 1024 * 1024

# Envelope headers worth surfacing, in the order a reader expects them.
HEADER_ORDER = (
    "From",
    "To",
    "Cc",
    "Bcc",
    "Reply-To",
    "Subject",
    "Date",
    "Message-ID",
)

# Any of these means the parser found real email structure even when every
# envelope header above is missing.
_STRUCTURAL_HEADERS = ("Content-Type", "MIME-Version", "Received", "Return-Path")

# An RFC 5322 header block is 7-bit text, so a NUL byte this early means the
# file is not email at all. Bounded to the header region so that a message with
# an 8-bit binary body cannot trip it.
_HEADER_SNIFF_BYTES = 1024


def _malformed_message(reason: str) -> str:
    """The refusal an agent sees when the bytes are not a usable email."""
    return (
        f"Cannot parse email file: {reason}. Confirm the file is a saved MIME email "
        "export (.eml); if it is some other format, read it under its real "
        "extension, or inspect the raw bytes with code execution."
    )


def oversize_message(file_path: str, file_size: int) -> str:
    """The refusal an agent sees when the .eml is larger than the parse limit."""
    return (
        f"Email file too large to parse: {file_path} is {file_size} bytes, over the "
        f"{EML_MAX_BYTES} byte ({EML_MAX_BYTES // (1024 * 1024)} MB) .eml limit. "
        "Use get_file_metadata to inspect it, or extract the part you need with "
        "code execution."
    )


def _collapse_whitespace(value: str) -> str:
    """Fold CR/LF/tab runs into single spaces so one header stays one line."""
    return " ".join(value.split())


def _repair_surrogates(raw_value: str) -> str:
    """Recover text from a header the parser surrogate-escaped.

    A header carrying raw 8-bit bytes (a `Subject:` written in UTF-8 without
    RFC 2047 encoding, which real mailers do emit) is decoded by the parser as
    ASCII with `surrogateescape`. Re-encoding recovers the original bytes.
    """
    try:
        return raw_value.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    except UnicodeEncodeError:
        return raw_value


def _decode_header_value(raw_value: str) -> str:
    """Decode RFC 2047 encoded words, falling back to the raw header text."""
    repaired = _repair_surrogates(raw_value)
    try:
        decoded = str(make_header(decode_header(repaired)))
    except (LookupError, UnicodeDecodeError, ValueError):
        decoded = repaired
    return _collapse_whitespace(decoded)


def _raw_headers(message: Message) -> dict[str, str]:
    """Header text by lower-cased name, first occurrence winning.

    `Message.get` runs compat32's sanitizer, which wraps any value carrying raw
    8-bit bytes in an `email.header.Header` whose `str()` has already replaced
    those bytes. `raw_items` hands back the undecoded string instead, so
    `_repair_surrogates` can still recover the text.
    """
    headers: dict[str, str] = {}
    for name, value in message.raw_items():
        key = name.lower()
        if key not in headers and isinstance(value, str):
            headers[key] = value
    return headers


def _decode_part_text(part: Message, fallback_encoding: str) -> str:
    """Decode one text part using its declared charset, never raising."""
    payload = part.get_payload(decode=True)
    if not isinstance(payload, bytes):
        return ""
    for charset in (part.get_content_charset(), fallback_encoding):
        if not charset:
            continue
        try:
            return payload.decode(charset, errors="replace")
        except LookupError:
            continue
    return payload.decode("utf-8", errors="replace")


def _sub_messages(message: Message) -> list[Message]:
    """The parts of a multipart message, or `[]` for a leaf."""
    payload = message.get_payload()
    if not isinstance(payload, list):
        return []
    # `is_multipart()` is defined as "the payload is a list of sub-messages",
    # so a list payload only ever holds Messages.
    return cast(list[Message], payload)


def _iter_leaves(message: Message) -> Iterator[Message]:
    """Yield leaf parts, treating an attached message as a single leaf.

    `Message.walk()` descends into attached messages, which would splice a
    forwarded email's body into the outer message's body sections. Every
    `message/*` subtype carries a nested message this way, not just
    `message/rfc822` -- `message/global` is the internationalized equivalent.
    """
    parts = (
        [] if message.get_content_maintype() == "message" else _sub_messages(message)
    )
    if not parts:
        yield message
        return
    for part in parts:
        yield from _iter_leaves(part)


def _is_attachment(part: Message) -> bool:
    if (part.get_content_disposition() or "").lower() == "attachment":
        return True
    if part.get_filename():
        return True
    return part.get_content_maintype() != "text"


def _message_bytes(message: Message) -> int:
    """Serialized length of a nested message, or 0 when it cannot be rendered."""
    try:
        return len(message.as_bytes())
    except Exception:
        return 0


def _attachment_size(part: Message) -> int:
    """Size of an attachment's decoded payload -- measured, never returned."""
    payload = part.get_payload(decode=True)
    if isinstance(payload, bytes):
        return len(payload)
    # Only a container payload (an attached message/rfc822) reaches here.
    return sum(_message_bytes(sub) for sub in _sub_messages(part))


def _attachment_line(part: Message) -> str:
    name = _decode_header_value(part.get_filename() or "")
    if not name:
        name = "(unnamed)"
    return f"- {name} ({part.get_content_type()}, {_attachment_size(part)} bytes)"


def render_eml(raw: bytes, fallback_encoding: str = "utf-8") -> str:
    """Render email bytes as headers, body text, and an attachment manifest.

    Args:
        raw: the file's bytes, already size-checked by the caller.
        fallback_encoding: charset for body parts that declare none.

    Returns:
        A text digest: `## Headers`, one `## Body` section per plain-text or
        HTML body part, `## Attachments`, and `## Warnings` when the parser
        recovered content from a malformed message.

    Raises:
        ValueError: the bytes are not a usable email -- no headers, no
            recoverable parts, or binary content where headers belong.
    """
    if b"\x00" in raw[:_HEADER_SNIFF_BYTES]:
        raise ValueError(
            _malformed_message("it starts with binary data, not email headers")
        )

    message = email.message_from_bytes(raw)
    headers = _raw_headers(message)

    header_lines: list[str] = []
    for name in HEADER_ORDER:
        value = headers.get(name.lower(), "")
        if value.strip():
            header_lines.append(f"{name}: {_decode_header_value(value)}")

    if not header_lines and not any(
        name.lower() in headers for name in _STRUCTURAL_HEADERS
    ):
        raise ValueError(_malformed_message("it carries no email headers at all"))

    bodies: list[tuple[str, str]] = []
    attachments: list[str] = []
    defect_names: list[str] = [type(defect).__name__ for defect in message.defects]

    for part in _iter_leaves(message):
        defect_names.extend(type(defect).__name__ for defect in part.defects)
        if part.get_content_maintype() == "multipart":
            # A container only reaches here when its boundary never parsed --
            # there is nothing to render, and the defects below say why.
            continue
        if _is_attachment(part):
            attachments.append(_attachment_line(part))
            continue
        text = _decode_part_text(part, fallback_encoding).strip()
        if text:
            bodies.append((part.get_content_type(), text))

    defect_names = list(dict.fromkeys(defect_names))

    if not bodies and not attachments:
        if defect_names:
            reason = (
                f"the parser reported {', '.join(defect_names)} and recovered no "
                "body or attachment"
            )
        else:
            reason = "it contains no readable body or attachment parts"
        raise ValueError(_malformed_message(reason))

    sections: list[str] = []
    if header_lines:
        sections.append("## Headers\n" + "\n".join(header_lines))

    for index, (content_type, text) in enumerate(bodies, start=1):
        label = f"## Body {index}" if len(bodies) > 1 else "## Body"
        note = ", raw source, not rendered" if content_type == "text/html" else ""
        sections.append(f"{label} ({content_type}{note})\n{text}")

    if attachments:
        sections.append(
            f"## Attachments ({len(attachments)})\n" + "\n".join(attachments)
        )

    if defect_names:
        sections.append(
            "## Warnings\nThis message is malformed; the parser recovered what it "
            "could and reported: " + ", ".join(defect_names)
        )

    return "\n\n".join(sections)
