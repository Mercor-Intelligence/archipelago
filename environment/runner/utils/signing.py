"""Ed25519 request-signature verification for incoming studio → runner calls.

When API_SIGNING_PUBLIC_KEY is set in settings, every non-exempt request must
carry three headers signed by the studio server's private key:

    X-Studio-Timestamp  Unix epoch seconds (integer string)
    X-Studio-Nonce      Random 32-hex-char string
    X-Studio-Signature  base64-encoded Ed25519 signature over the payload

Signed payload (UTF-8 bytes):
    "{METHOD}\\n{path}\\n{timestamp}\\n{nonce}\\n{sha256_hex}"

where sha256_hex is the lowercase hex SHA-256 digest of the raw request body
(empty string digest when the body is absent).

Requests with a stale timestamp (>5 min) or an invalid signature are rejected
with 403. Signing is completely optional; omitting API_SIGNING_PUBLIC_KEY leaves
all existing behaviour unchanged (OSS / delivery compatibility).
"""

import base64
import hashlib
import time
from functools import cache

from loguru import logger

from .settings import get_settings

_TIMESTAMP_TOLERANCE_SECONDS = 300  # 5 minutes


@cache
def _get_public_key():
    """Load Ed25519 public key from settings. Cached after first call.

    Returns None when API_SIGNING_PUBLIC_KEY is not set (signing disabled).
    Raises RuntimeError on a malformed key so the startup error is visible.
    """
    settings = get_settings()
    if not settings.API_SIGNING_PUBLIC_KEY:
        return None
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # type: ignore[import-untyped]
            Ed25519PublicKey,
        )

        raw = base64.b64decode(settings.API_SIGNING_PUBLIC_KEY)
        return Ed25519PublicKey.from_public_bytes(raw)
    except Exception as exc:
        raise RuntimeError(f"Invalid API_SIGNING_PUBLIC_KEY: {exc}") from exc


def signing_enabled() -> bool:
    """Return True when signature enforcement is active."""
    return _get_public_key() is not None


def verify_request_signature(
    method: str,
    path: str,
    timestamp_str: str,
    nonce: str,
    signature_b64: str,
    body: bytes = b"",
) -> bool:
    """Verify an Ed25519 request signature.

    Logs the raw signature, the reconstructed payload, and the match result
    at DEBUG level for troubleshooting. Intended to be called from the
    FastAPI middleware before any handler runs.

    Returns True when the signature is valid, False otherwise.
    Always returns True when signing is not enabled.
    """
    public_key = _get_public_key()
    if public_key is None:
        return True

    try:
        ts = int(timestamp_str)
    except (ValueError, TypeError):
        logger.debug(
            "Signature check FAILED: unparseable timestamp={!r}", timestamp_str
        )
        return False

    age = abs(time.time() - ts)
    if age > _TIMESTAMP_TOLERANCE_SECONDS:
        logger.debug("Signature check FAILED: stale timestamp age={:.1f}s", age)
        return False

    body_hash = hashlib.sha256(body).hexdigest()
    payload = (
        f"{method.upper()}\n{path}\n{timestamp_str}\n{nonce}\n{body_hash}".encode()
    )

    try:
        sig_bytes = base64.b64decode(signature_b64)
    except Exception:
        logger.debug("Signature check FAILED: invalid base64 in signature header")
        return False

    try:
        from cryptography.exceptions import (  # type: ignore[import-untyped]
            InvalidSignature,
        )
    except ImportError:
        logger.debug("Signature check FAILED: cryptography library not available")
        return False

    try:
        public_key.verify(sig_bytes, payload)
        logger.debug("Signature check PASSED: method={} path={}", method, path)
        return True
    except InvalidSignature:
        logger.debug("Signature check FAILED: signature does not match payload")
        return False
    except Exception as exc:
        logger.debug("Signature check FAILED: unexpected error: {}", exc)
        return False
