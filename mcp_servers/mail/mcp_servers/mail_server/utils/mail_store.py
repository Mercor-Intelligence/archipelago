"""Discovery of every mail source stored under the mail data root.

A mailbox is not one file. A world can seed it as an mbox, as individual
RFC 5322 ``.eml`` messages (often foldered, e.g. ``inbox/``), or as a mixture,
and messages this server sends are appended to the mbox ``get_mbox_path``
selects. Reads therefore span every source under ``MAIL_DATA_ROOT``; writes
still target that single mbox.

A source that cannot be read is reported rather than skipped, so an unreadable
file surfaces as an error instead of an empty inbox.
"""

import hashlib
import os
import re
from dataclasses import dataclass, field
from email import message_from_bytes
from email.message import Message

from utils.mbox_utils import UTF8Mbox, message_identity, safe_get_header
from utils.path import resolve_mail_path

MBOX_SUFFIX = ".mbox"
EML_SUFFIX = ".eml"

# Headers that distinguish an RFC 5322 message from an arbitrary file that
# happens to carry a .eml name.
_IDENTIFYING_HEADERS = ("from", "to", "cc", "subject", "date", "message-id")

# Start of a header block, used to find the message inside a file that opens
# with a banner or separator rather than with the headers themselves.
_HEADER_BLOCK_START = re.compile(
    rb"^(?:Return-Path|Received|Message-ID|From|To|Cc|Subject|Date|"
    rb"MIME-Version|Content-Type|Sender|Reply-To|X-[A-Za-z0-9-]+):",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass(frozen=True)
class LoadedMail:
    """One message plus where it came from."""

    message: Message
    mail_id: str
    source: str
    synthesized_id: bool


@dataclass
class MailboxLoad:
    """Everything readable under the mail data root, and what was not."""

    mails: list[LoadedMail] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    _seen: dict[str, str] = field(default_factory=dict, repr=False)

    @property
    def error_text(self) -> str | None:
        """Errors joined into one actionable message, or None when all sources read."""
        if not self.errors:
            return None
        return "; ".join(self.errors)

    @property
    def warning_text(self) -> str | None:
        """Everything the caller should know that did not stop the read.

        An unreadable source is a warning rather than an error whenever
        something else did read, because the returned mail is accurate for
        every source that worked.
        """
        parts = ([] if self.scan_failed else list(self.errors)) + list(self.warnings)
        if not parts:
            return None
        return "; ".join(parts)

    @property
    def scan_failed(self) -> bool:
        """True when something failed and nothing at all could be read.

        Distinguishes a mailbox whose contents are unknown from one that was
        read despite an unreadable file in it - in the second case an empty
        result is a real 'no matches', not a failure.
        """
        return bool(self.errors) and not self.mails


def synthesize_mail_id(source: str, index: int) -> str:
    """Build a stable Message-ID for a message that carries none.

    Derived from the source path and the message's position within it, so the
    same seed yields the same identifier across restarts, populate runs, and
    snapshot/restore.
    """
    digest = hashlib.sha1(f"{source}#{index}".encode()).hexdigest()[:20]
    return f"<seed-{digest}@mail.invalid>"


def _relative(path: str) -> str:
    try:
        return os.path.relpath(path, resolve_mail_path(""))
    except ValueError:
        return path


def _looks_like_message(message: Message) -> bool:
    return any(message.get(header) is not None for header in _IDENTIFYING_HEADERS)


def _collect(load: MailboxLoad, message: Message, source: str, index: int) -> None:
    mail_id = safe_get_header(message, "Message-ID")
    synthesized = not mail_id
    if synthesized:
        mail_id = synthesize_mail_id(source, index)

    identity = message_identity(message)
    seen_identity = load._seen.get(mail_id)
    if seen_identity is not None:
        if seen_identity == identity:
            # The same message seeded twice, e.g. as a loose .eml and again
            # inside an mbox. One copy, listed once.
            return
        # Two different messages claiming one Message-ID. Dropping the second
        # would make a real message undiscoverable with nothing said about it,
        # so it is kept under a distinct id derived from where it was found.
        disambiguated = synthesize_mail_id(f"{source}#{mail_id}", index)
        load.warnings.append(
            f"'{source}' reuses the Message-ID {mail_id} of an earlier message; "
            f"it is listed as {disambiguated}"
        )
        mail_id = disambiguated
        synthesized = True

    load._seen[mail_id] = identity
    load.mails.append(
        LoadedMail(
            message=message,
            mail_id=mail_id,
            source=source,
            synthesized_id=synthesized,
        )
    )


def _load_mbox(load: MailboxLoad, path: str) -> None:
    source = _relative(path)
    box = UTF8Mbox(path)
    try:
        box.lock()
    except (BlockingIOError, OSError):
        load.errors.append(
            f"Mailbox '{source}' is currently busy. Please try again in a moment."
        )
        return
    try:
        for index, message in enumerate(box):
            _collect(load, message, source, index)
    finally:
        box.unlock()
        box.close()


def _parse_eml(raw: bytes) -> Message | None:
    """Parse one .eml, tolerating text that precedes the header block.

    Seeded files are sometimes filed under a privilege banner or separator
    line. The headers are still there, just not at byte zero, and rejecting
    the whole file over the preamble hides a real message.
    """
    message = message_from_bytes(raw)
    if _looks_like_message(message):
        return message
    match = _HEADER_BLOCK_START.search(raw)
    if match is None or match.start() == 0:
        return None
    message = message_from_bytes(raw[match.start() :])
    return message if _looks_like_message(message) else None


def _load_eml(load: MailboxLoad, path: str) -> None:
    source = _relative(path)
    with open(path, "rb") as handle:
        message = _parse_eml(handle.read())
    if message is None:
        raise ValueError(
            "file carries no email headers, so it is not an RFC 5322 message"
        )
    _collect(load, message, source, 0)


def load_mailbox() -> MailboxLoad:
    """Read every mail source under the mail data root.

    Sources are visited in sorted order so identifiers and list ordering are
    reproducible. The first source to claim a Message-ID wins, so a message
    seeded twice is listed once.
    """
    load = MailboxLoad()
    mail_root = resolve_mail_path("")
    if not os.path.isdir(mail_root):
        return load

    for root, dirs, files in os.walk(mail_root):
        dirs.sort()
        for name in sorted(files):
            path = os.path.join(root, name)
            lowered = name.lower()
            if lowered.endswith(MBOX_SUFFIX):
                reader = _load_mbox
            elif lowered.endswith(EML_SUFFIX):
                reader = _load_eml
            else:
                continue
            try:
                reader(load, path)
            except Exception as exc:
                load.errors.append(
                    f"Could not read mail source '{_relative(path)}': {exc!r}"
                )
    return load


def find_mail(mail_id: str) -> tuple[LoadedMail | None, MailboxLoad]:
    """Locate one message by Message-ID across every source."""
    load = load_mailbox()
    for loaded in load.mails:
        if loaded.mail_id == mail_id:
            return loaded, load
    return None, load
