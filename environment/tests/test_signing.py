"""Tests for the X-Studio-Signature request-signing middleware.

Spins up the environment container with API_SIGNING_PUBLIC_KEY set and verifies
that unsigned or tampered requests are rejected while correctly signed requests
pass through.
"""

from __future__ import annotations

import asyncio
import base64
import os
import time
from collections.abc import Generator
from pathlib import Path

import httpx
import pytest
from testcontainers.core.container import DockerContainer

TESTS_DIR = Path(__file__).parent
ENVIRONMENT_DIR = TESTS_DIR.parent


# ---------------------------------------------------------------------------
# Key helpers (inline — no server-side dependency)
# ---------------------------------------------------------------------------


def _generate_key_pair() -> tuple[str, str]:
    """Return (private_b64, public_b64) for a fresh Ed25519 key pair."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.generate()
    return (
        base64.b64encode(priv.private_bytes_raw()).decode(),
        base64.b64encode(priv.public_key().public_bytes_raw()).decode(),
    )


def _sign(
    private_b64: str, method: str, path: str, body: bytes = b""
) -> dict[str, str]:
    """Return signing headers for a request."""
    import hashlib

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    raw = base64.b64decode(private_b64)
    key = Ed25519PrivateKey.from_private_bytes(raw)
    timestamp = str(int(time.time()))
    nonce = os.urandom(16).hex()
    body_hash = hashlib.sha256(body).hexdigest()
    payload = f"{method.upper()}\n{path}\n{timestamp}\n{nonce}\n{body_hash}".encode()
    sig = key.sign(payload)
    return {
        "X-Studio-Timestamp": timestamp,
        "X-Studio-Nonce": nonce,
        "X-Studio-Signature": base64.b64encode(sig).decode(),
    }


# ---------------------------------------------------------------------------
# Signed-container fixture (class-scoped so we build the image once per class)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="class")
def signing_key_pair() -> tuple[str, str]:
    return _generate_key_pair()


@pytest.fixture(scope="class")
def signed_base_url(
    environment_image: str,
    signing_key_pair: tuple[str, str],
) -> Generator[tuple[str, str]]:
    """Start the container with API_SIGNING_PUBLIC_KEY; yield (base_url, private_b64)."""
    private_b64, public_b64 = signing_key_pair

    with (
        DockerContainer(image=environment_image)
        .with_exposed_ports(8080)
        .with_env("ENV", "local")
        .with_env("S3_DEFAULT_REGION", "us-west-2")
        .with_env("S3_SNAPSHOTS_BUCKET", "test")
        .with_env("API_SIGNING_PUBLIC_KEY", public_b64)
    ) as container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(8080)
        base_url = f"http://{host}:{port}"

        loop = asyncio.new_event_loop()
        try:

            async def _wait() -> bool:
                async with httpx.AsyncClient() as c:
                    for _ in range(60):
                        try:
                            r = await c.get(f"{base_url}/health", timeout=3)
                            if r.status_code == 200:
                                return True
                        except Exception:
                            pass
                        await asyncio.sleep(2)
                return False

            ok = loop.run_until_complete(_wait())
            if not ok:
                pytest.fail("Signed environment container did not start in time")
        finally:
            loop.close()

        yield base_url, private_b64


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSignatureEnforcement:
    """Signing middleware rejects unauthenticated requests and accepts signed ones."""

    @pytest.mark.asyncio
    async def test_health_passes_without_signature(
        self, signed_base_url: tuple[str, str]
    ) -> None:
        """Health endpoint is exempt from signing — used by container orchestrators."""
        base_url, _ = signed_base_url
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{base_url}/health", timeout=10)
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_snapshot_rejected_without_signature(
        self, signed_base_url: tuple[str, str]
    ) -> None:
        """POST /data/snapshot without signing headers returns 403."""
        base_url, _ = signed_base_url
        async with httpx.AsyncClient() as client:
            response = await client.post(f"{base_url}/data/snapshot", timeout=10)
        assert response.status_code == 403, (
            f"Expected 403, got {response.status_code}: {response.text}"
        )

    @pytest.mark.asyncio
    async def test_populate_rejected_without_signature(
        self, signed_base_url: tuple[str, str]
    ) -> None:
        """POST /data/populate/s3 without signing headers returns 403."""
        base_url, _ = signed_base_url
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{base_url}/data/populate/s3",
                json={"sources": []},
                timeout=10,
            )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_snapshot_accepted_with_valid_signature(
        self, signed_base_url: tuple[str, str]
    ) -> None:
        """POST /data/snapshot with correct signature passes the middleware (may fail downstream)."""
        base_url, private_b64 = signed_base_url
        headers = _sign(private_b64, "POST", "/data/snapshot")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{base_url}/data/snapshot",
                headers=headers,
                timeout=30,
            )
        # 403 means rejected by middleware — anything else means middleware passed it
        assert response.status_code != 403, (
            f"Valid signature was rejected: {response.text}"
        )

    @pytest.mark.asyncio
    async def test_populate_accepted_with_valid_signature(
        self, signed_base_url: tuple[str, str]
    ) -> None:
        """POST /data/populate/s3 with correct signature passes the middleware."""
        import json

        base_url, private_b64 = signed_base_url
        body = json.dumps({"sources": []}).encode()
        headers = _sign(private_b64, "POST", "/data/populate/s3", body=body)
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{base_url}/data/populate/s3",
                content=body,
                headers={**headers, "content-type": "application/json"},
                timeout=30,
            )
        assert response.status_code != 403, (
            f"Valid signature was rejected: {response.text}"
        )

    @pytest.mark.asyncio
    async def test_wrong_key_rejected(self, signed_base_url: tuple[str, str]) -> None:
        """A signature from an unknown key is rejected with 403."""
        base_url, _ = signed_base_url
        wrong_private_b64, _ = _generate_key_pair()
        headers = _sign(wrong_private_b64, "POST", "/data/snapshot")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{base_url}/data/snapshot",
                headers=headers,
                timeout=10,
            )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_tampered_signature_rejected(
        self, signed_base_url: tuple[str, str]
    ) -> None:
        """A valid signature applied to a different path is rejected."""
        base_url, private_b64 = signed_base_url
        # Sign for /data/populate but send to /data/snapshot
        headers = _sign(private_b64, "POST", "/data/populate")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{base_url}/data/snapshot",
                headers=headers,
                timeout=10,
            )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_stale_timestamp_rejected(
        self, signed_base_url: tuple[str, str]
    ) -> None:
        """A signature with a timestamp older than 5 minutes is rejected."""
        base_url, private_b64 = signed_base_url
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        raw = base64.b64decode(private_b64)
        key = Ed25519PrivateKey.from_private_bytes(raw)
        # 6 minutes in the past
        import hashlib

        old_ts = str(int(time.time()) - 360)
        nonce = os.urandom(16).hex()
        body_hash = hashlib.sha256(b"").hexdigest()
        payload = f"POST\n/data/snapshot\n{old_ts}\n{nonce}\n{body_hash}".encode()
        sig = base64.b64encode(key.sign(payload)).decode()

        headers = {
            "X-Studio-Timestamp": old_ts,
            "X-Studio-Nonce": nonce,
            "X-Studio-Signature": sig,
        }
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{base_url}/data/snapshot",
                headers=headers,
                timeout=10,
            )
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# Unit tests for signing utilities (no container required)
# ---------------------------------------------------------------------------


class TestSigningUtils:
    """Unit tests for runner/utils/signing.py — no container needed."""

    def _make_fake_settings(self, pub_b64: str | None):  # type: ignore[no-untyped-def]
        import unittest.mock as mock

        fake = mock.MagicMock()
        fake.API_SIGNING_PUBLIC_KEY = pub_b64
        return fake

    def test_verify_valid_signature(self) -> None:
        import unittest.mock as mock

        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        import runner.utils.signing as signing_module
        from runner.utils.signing import verify_request_signature

        priv = Ed25519PrivateKey.generate()
        pub_b64 = base64.b64encode(priv.public_key().public_bytes_raw()).decode()

        import hashlib

        method, path = "POST", "/data/snapshot"
        timestamp = str(int(time.time()))
        nonce = os.urandom(16).hex()
        body_hash = hashlib.sha256(b"").hexdigest()
        payload = f"{method}\n{path}\n{timestamp}\n{nonce}\n{body_hash}".encode()
        sig_b64 = base64.b64encode(priv.sign(payload)).decode()

        signing_module._get_public_key.cache_clear()
        with mock.patch.object(
            signing_module,
            "get_settings",
            return_value=self._make_fake_settings(pub_b64),
        ):
            result = verify_request_signature(method, path, timestamp, nonce, sig_b64)
        signing_module._get_public_key.cache_clear()

        assert result is True

    def test_verify_invalid_signature(self) -> None:
        import unittest.mock as mock

        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        import runner.utils.signing as signing_module
        from runner.utils.signing import verify_request_signature

        priv = Ed25519PrivateKey.generate()
        pub_b64 = base64.b64encode(priv.public_key().public_bytes_raw()).decode()

        import hashlib

        method, path = "POST", "/data/snapshot"
        timestamp = str(int(time.time()))
        nonce = os.urandom(16).hex()
        # Signed for a different path → should fail verification on the correct path
        body_hash = hashlib.sha256(b"").hexdigest()
        payload = f"{method}\n/wrong/path\n{timestamp}\n{nonce}\n{body_hash}".encode()
        sig_b64 = base64.b64encode(priv.sign(payload)).decode()

        signing_module._get_public_key.cache_clear()
        with mock.patch.object(
            signing_module,
            "get_settings",
            return_value=self._make_fake_settings(pub_b64),
        ):
            result = verify_request_signature(method, path, timestamp, nonce, sig_b64)
        signing_module._get_public_key.cache_clear()

        assert result is False

    def test_verify_disabled_when_no_key(self) -> None:
        import unittest.mock as mock

        import runner.utils.signing as signing_module
        from runner.utils.signing import verify_request_signature

        signing_module._get_public_key.cache_clear()
        with mock.patch.object(
            signing_module, "get_settings", return_value=self._make_fake_settings(None)
        ):
            result = verify_request_signature(
                "POST", "/data/snapshot", "123", "nonce", "sig"
            )
        signing_module._get_public_key.cache_clear()

        assert result is True
