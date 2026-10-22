"""Opening password-protected PDFs, and the messages for when that fails.

pypdf and PyMuPDF both try the empty password when a document is opened without
one, so a file carrying only an owner password reads normally with no password
argument at all. A document whose *user* password is set needs that password:
the caller passes it in the tools' ``password`` argument and
:func:`unlock_pdf_bytes` decrypts the document with pypdf.

PyMuPDF is never asked to decrypt. On real task documents (AES-128 and RC4-128)
PyMuPDF 1.26.7's ``Document.authenticate()`` returns a success code while
``needs_pass`` stays set and ``get_text()`` returns ``""``, which would turn a
correct password back into a silent zero-match search. Every PyMuPDF path
instead opens the plain bytes pypdf writes out after decrypting.

The password itself never appears in a message, a log line, or a cache key:
:func:`password_cache_token` is a keyed hash whose key is random per process.
"""

import hashlib
import hmac
import io
import secrets

from pypdf import PasswordType, PdfReader, PdfWriter

PASSWORD_PROTECTED_NOTE = (
    "the document is password-protected and the empty password did not open it; "
    "pass the document's password in the `password` argument"
)

PASSWORD_INCORRECT_NOTE = (
    "the document is password-protected and the password supplied in the "
    "`password` argument is incorrect"
)


class PdfPasswordRequiredError(ValueError):
    """The document needs a user password and none was supplied."""

    def __init__(self) -> None:
        super().__init__(PASSWORD_PROTECTED_NOTE)


class PdfPasswordIncorrectError(ValueError):
    """A password was supplied and it does not open the document."""

    def __init__(self) -> None:
        super().__init__(PASSWORD_INCORRECT_NOTE)


def password_protected_error(file_path: str) -> str:
    """The caller-facing error for a protected document opened without a password."""
    return f"Cannot read {file_path}: {PASSWORD_PROTECTED_NOTE}."


def password_incorrect_error(file_path: str) -> str:
    """The caller-facing error for a protected document opened with a wrong password."""
    return f"Cannot read {file_path}: {PASSWORD_INCORRECT_NOTE}."


def password_error_message(file_path: str, exc: ValueError) -> str:
    """The caller-facing message for either password exception."""
    if isinstance(exc, PdfPasswordIncorrectError):
        return password_incorrect_error(file_path)
    return password_protected_error(file_path)


def unlock_pdf_bytes(file_bytes: bytes, password: str | None) -> tuple[bytes, bool]:
    """Return bytes both pypdf and PyMuPDF can open without a password.

    Returns ``(pdf_bytes, unlocked_with_password)``. The input comes back
    unchanged when the document is not encrypted or the empty password opens
    it -- the behaviour every tool already had -- and ``password`` is then
    ignored. Otherwise the document is decrypted with ``password`` and
    re-serialised unencrypted, in memory only, for this one call.

    Raises:
        PdfPasswordRequiredError: a user password is needed and none was given.
        PdfPasswordIncorrectError: ``password`` does not open the document.
    """
    reader = PdfReader(io.BytesIO(file_bytes))
    if not reader.is_encrypted:
        return file_bytes, False
    if reader.decrypt("") != PasswordType.NOT_DECRYPTED:
        return file_bytes, False
    if not password:
        raise PdfPasswordRequiredError()

    reader = PdfReader(io.BytesIO(file_bytes))
    if reader.decrypt(password) == PasswordType.NOT_DECRYPTED:
        raise PdfPasswordIncorrectError()

    writer = PdfWriter(clone_from=reader)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue(), True


# Random per process, so a token cannot be checked against password guesses
# offline and does not survive a restart.
_CACHE_TOKEN_KEY = secrets.token_bytes(32)


def password_cache_token(password: str) -> str:
    """An opaque cache-key component for content unlocked with ``password``."""
    digest = hmac.new(_CACHE_TOKEN_KEY, password.encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()[:32]


def image_cache_prefix(physical_path: str, unlock_password: str | None) -> str:
    """The image-cache key prefix for a document.

    ``unlock_password`` is the password that was *needed* to open the document,
    or ``None`` when it opens without one. Images from a document that needed a
    password are filed under a prefix derived from that password, so a later
    ``read_image`` call that does not supply it cannot be served them from the
    cache: it misses, re-opens the document, and gets the password error.
    """
    if not unlock_password:
        return physical_path
    return f"{physical_path}#pw={password_cache_token(unlock_password)}"
