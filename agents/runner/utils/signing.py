"""Ed25519 request signing for agent → environment runner HTTP calls.

When API_SIGNING_PRIVATE_KEY is set in settings, outgoing calls to the
environment runner are augmented with three authentication headers:

    X-Studio-Timestamp  Unix epoch seconds (integer string)
    X-Studio-Nonce      Random 32-hex-char string
    X-Studio-Signature  base64-encoded Ed25519 signature over the payload

Signed payload (UTF-8 bytes):
    "{METHOD}\\n{path}\\n{timestamp}\\n{nonce}\\n{sha256_hex}"

where sha256_hex is the lowercase hex SHA-256 digest of the raw request body
(empty string digest when the body is absent).

Call ``sign_env_runner_request(method, url, body=<bytes>)`` and merge the result
into your outgoing request headers. Returns an empty dict when signing is not
configured, preserving the existing unauthenticated behaviour.
"""

import base64
import hashlib
import os
import time
from functools import cache
from urllib.parse import urlparse

from loguru import logger

from .settings import get_settings


@cache
def _get_private_key():
    """Load Ed25519 private key from settings. Cached after first call.

    Returns None when API_SIGNING_PRIVATE_KEY is not set (signing disabled).
    """
    settings = get_settings()
    if not settings.API_SIGNING_PRIVATE_KEY:
        return None
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # type: ignore[import-untyped]
            Ed25519PrivateKey,
        )

        raw = base64.b64decode(settings.API_SIGNING_PRIVATE_KEY)
        return Ed25519PrivateKey.from_private_bytes(raw)
    except Exception as exc:
        logger.warning("Failed to load API_SIGNING_PRIVATE_KEY: {}", exc)
        return None


def validate_signing_keypair(
    private_b64: str | None, public_b64: str | None
) -> str | None:
    """Return public_b64 if the keypair is consistent, None otherwise.

    Logs an error when both keys are present but don't match so a misconfigured
    deploy is caught before the public key is injected into a sandbox (which
    would cause the sandbox to reject every subsequent request with 403).
    """
    if not private_b64 or not public_b64:
        return None
    try:
        from cryptography.exceptions import (
            InvalidSignature,  # type: ignore[import-untyped]
        )
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # type: ignore[import-untyped]
            Ed25519PrivateKey,
            Ed25519PublicKey,
        )
    except ImportError as exc:
        logger.error("Failed to validate signing keypair: {} — signing disabled", exc)
        return None
    try:
        priv = Ed25519PrivateKey.from_private_bytes(base64.b64decode(private_b64))
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_b64))
        probe = b"keypair-validation-probe"
        pub.verify(priv.sign(probe), probe)
        return public_b64
    except InvalidSignature:
        logger.error(
            "API_SIGNING_PRIVATE_KEY and API_SIGNING_PUBLIC_KEY do not match — "
            "signing disabled to prevent environment sandboxes rejecting all requests"
        )
        return None
    except Exception as exc:
        logger.error("Failed to validate signing keypair: {} — signing disabled", exc)
        return None


def sign_env_runner_request(method: str, url: str, body: bytes = b"") -> dict[str, str]:
    """Build X-Studio-* signing headers for an outgoing env-runner HTTP call.

    Args:
        method: HTTP method (e.g. ``"POST"``).
        url: Full request URL. The path (and query string if present) is
             extracted and included in the signed payload.
        body: Raw request body bytes. Must be the exact bytes that will be sent
              so the runner can verify the signature. Defaults to ``b""`` for
              requests with no body.

    Returns:
        Dict with ``X-Studio-Timestamp``, ``X-Studio-Nonce``, and
        ``X-Studio-Signature`` ready to be merged into the request headers,
        or an empty dict when ``API_SIGNING_PRIVATE_KEY`` is not configured.
    """
    key = _get_private_key()
    if key is None:
        return {}

    parsed = urlparse(url)
    path = parsed.path
    if parsed.query:
        path = f"{path}?{parsed.query}"

    timestamp = str(int(time.time()))
    nonce = os.urandom(16).hex()
    body_hash = hashlib.sha256(body).hexdigest()
    payload = f"{method.upper()}\n{path}\n{timestamp}\n{nonce}\n{body_hash}".encode()

    try:
        sig = key.sign(payload)
        return {
            "X-Studio-Timestamp": timestamp,
            "X-Studio-Nonce": nonce,
            "X-Studio-Signature": base64.b64encode(sig).decode(),
        }
    except Exception as exc:
        logger.warning("Failed to sign env runner request: {}", exc)
        return {}
