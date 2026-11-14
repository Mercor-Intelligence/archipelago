"""S3 client utilities for interacting with S3-compatible storage.

Credential priority: explicit S3Credentials → OIDC exchange (MODAL_IDENTITY_TOKEN +
MODAL_OIDC_ROLE_ARN) → boto3 default chain (AWS_* env vars, instance metadata, etc.).

Explicit credentials come in two flavours. A plain :class:`S3Credentials` is
frozen into the session at build time — correct for short operations. A
:class:`RefreshableS3Credentials` is a *holder* the caller can rotate mid-flight
(see ``data.snapshot.jobs``), for operations that outlive one STS lease.
"""

import asyncio
import datetime as dt
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import aioboto3
import aiobotocore.session
from aiobotocore.config import AioConfig
from aiobotocore.credentials import AioRefreshableCredentials
from loguru import logger
from pydantic import BaseModel, Field, SecretStr, field_validator
from types_aiobotocore_s3.service_resource import S3ServiceResource

from runner.utils.settings import get_settings

settings = get_settings()


class S3Credentials(BaseModel):
    """S3 credentials to use for the populate operation."""

    access_key_id: str = Field(..., description="AWS access key ID")
    secret_access_key: SecretStr = Field(..., description="AWS secret access key")
    session_token: SecretStr | None = Field(
        default=None, description="AWS session token (optional but recommended)"
    )
    region: str = Field(default=settings.S3_DEFAULT_REGION, description="AWS region")

    @field_validator("region")
    @classmethod
    def validate_region(cls, v: str) -> str:
        """Validate that the region is a valid AWS region."""
        if not v or not v.strip():
            raise ValueError("Region cannot be empty")
        return v.strip()


class SnapshotCredentialsExpired(RuntimeError):
    """The held lease lapsed and no refresh arrived in time.

    Raised out of the refresh callback so the caller sees *this* rather than
    botocore's generic "Credentials were refreshed, but the refreshed
    credentials are still expired" RuntimeError — which is what surfaces
    otherwise, and which no error classifier can safely recognise. The snapshot
    uploader treats it as retryable so a push that lands moments late still
    rescues the file.
    """


# How close to expiry the callback starts refusing the lease. botocore raises
# its own opaque RuntimeError the instant `_is_expired()` turns true during a
# mandatory refresh, so the callback has to fail *first* to keep the error
# typed. A few seconds is enough to win that race deterministically without
# shortening the usable life of a lease that is minutes long.
_LEASE_GUARD_SECONDS = 5.0


class RefreshableS3Credentials:
    """A mutable credential slot a long-running upload signs against.

    The credentials handed to this sandbox are a scoped STS lease that role
    chaining caps at 3600s (see ``generate_snapshot_credentials``), and they are
    minted *before* the work starts. Any upload that outlives the lease fails on
    its remaining PUTs — the worst possible place, since it is discovered only
    after the whole harvest has been paid for. The largest single DB in a
    warehouse-scale world is already past that ceiling on its own at the
    5 MiB/s floor the per-file timeout assumes.

    Rather than raise the TTL (which only moves the ceiling — files keep
    growing), the credentials become rotatable in place: botocore resolves and
    signs against the credential object on *every* request, so a multipart
    upload picks up whatever this holder contains for each subsequent
    ``UploadPart``. The caller pushes a fresh lease over its existing status-poll
    channel (``POST /data/snapshot/s3/credentials/{job_id}``) before the current
    one expires.

    :meth:`update` is called from the request handler while the upload runs, so
    reads and writes are guarded — the refresh callback runs on the same event
    loop, but the holder is also read from botocore's own refresh lock.
    """

    def __init__(self, credentials: S3Credentials, expires_at: dt.datetime) -> None:
        self._credentials = credentials
        self._expires_at = _as_utc(expires_at)
        self._lock = asyncio.Lock()
        self.refresh_count = 0

    @property
    def region(self) -> str:
        return self._credentials.region

    @property
    def expires_at(self) -> dt.datetime:
        return self._expires_at

    async def update(self, credentials: S3Credentials, expires_at: dt.datetime) -> None:
        """Swap in a freshly minted lease for the rest of the operation.

        Ignores a push that would move expiry *backwards*: retries and
        out-of-order deliveries are both possible on the caller's side, and
        replacing a live lease with a staler one would be strictly worse than
        doing nothing.
        """
        expiry = _as_utc(expires_at)
        async with self._lock:
            if expiry <= self._expires_at:
                logger.warning(
                    f"Ignoring snapshot credential refresh expiring at {expiry} — "
                    f"not newer than the current lease ({self._expires_at})"
                )
                return
            self._credentials = credentials
            self._expires_at = expiry
            self.refresh_count += 1
        logger.info(
            f"Snapshot credentials refreshed (#{self.refresh_count}); "
            f"lease now expires at {expiry.isoformat()}"
        )

    def _metadata(self) -> dict[str, str]:
        creds = self._credentials
        token = creds.session_token
        return {
            "access_key": creds.access_key_id,
            "secret_key": creds.secret_access_key.get_secret_value(),
            "token": token.get_secret_value() if token is not None else "",
            "expiry_time": self._expires_at.isoformat(),
        }

    async def _refresh(self) -> dict[str, str]:
        """botocore's refresh hook: hand back whatever was last pushed.

        Deliberately does no I/O — this sandbox holds no minting identity of its
        own, so the freshest lease it can offer is the one the caller pushed. If
        a refresh has not landed yet this returns the current lease and botocore
        asks again on the next request, which is the normal case: the callback
        is invoked constantly once inside botocore's refresh window and almost
        always has nothing new to say.

        Once the lease is actually spent, raising beats returning it. botocore
        swallows a failed *advisory* refresh (the request proceeds on the old
        lease and S3 answers ``ExpiredToken``, which is retryable) and
        propagates a failed *mandatory* one — so either way the uploader gets an
        error it can classify and retry, instead of the opaque RuntimeError
        botocore raises when it notices the expiry itself.
        """
        async with self._lock:
            remaining = (self._expires_at - dt.datetime.now(dt.UTC)).total_seconds()
            if remaining <= _LEASE_GUARD_SECONDS:
                raise SnapshotCredentialsExpired(
                    f"snapshot S3 lease expired at {self._expires_at.isoformat()} "
                    f"and no refresh arrived ({self.refresh_count} received so far)"
                )
            return self._metadata()

    def as_botocore_credentials(self) -> AioRefreshableCredentials:
        return AioRefreshableCredentials.create_from_metadata(
            metadata=self._metadata(),
            refresh_using=self._refresh,
            method="snapshot-credential-push",
        )


def _as_utc(value: dt.datetime) -> dt.datetime:
    """Normalize to an aware UTC datetime — botocore compares against aware now()."""
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _get_s3_session(
    credentials: S3Credentials | RefreshableS3Credentials | None = None,
) -> aioboto3.Session:
    """Build an aioboto3 session.

    Priority: explicit credentials → OIDC exchange → default chain (includes AWS_* env vars, local dev).
    """
    if isinstance(credentials, RefreshableS3Credentials):
        logger.debug("S3 setup using refreshable (caller-pushed) credentials")
        # Assigning `_credentials` short-circuits the provider chain, so the
        # session resolves to this object and every signed request — including
        # each multipart part — re-reads it.
        botocore_session = aiobotocore.session.get_session()
        botocore_session._credentials = credentials.as_botocore_credentials()  # pyright: ignore[reportAttributeAccessIssue]
        return aioboto3.Session(
            botocore_session=botocore_session, region_name=credentials.region
        )

    if credentials:
        logger.debug("S3 setup using explicit credentials")
        return aioboto3.Session(
            aws_access_key_id=credentials.access_key_id,
            aws_secret_access_key=credentials.secret_access_key.get_secret_value(),
            aws_session_token=(
                credentials.session_token.get_secret_value()
                if credentials.session_token is not None
                else None
            ),
            region_name=credentials.region,
        )

    oidc_token = os.environ.get("MODAL_IDENTITY_TOKEN")
    role_arn = os.environ.get("MODAL_OIDC_ROLE_ARN")

    if not oidc_token or not role_arn:
        if os.environ.get("MODAL_IS_REMOTE") and not (
            os.environ.get("AWS_ACCESS_KEY_ID")
            and os.environ.get("AWS_SECRET_ACCESS_KEY")
        ):
            raise RuntimeError(
                "Running on Modal without pre-scoped AWS credentials or OIDC token. Set AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN, or set MODAL_IDENTITY_TOKEN and MODAL_OIDC_ROLE_ARN."
            )
        return aioboto3.Session()

    import boto3

    logger.debug(f"S3 setup assuming OIDC role with ARN: {role_arn}")

    try:
        sts = boto3.client("sts", region_name=settings.S3_DEFAULT_REGION)
        resp = sts.assume_role_with_web_identity(  # pyright: ignore[reportAttributeAccessIssue]
            RoleArn=role_arn,
            RoleSessionName="modal-oidc-environment",
            WebIdentityToken=oidc_token,
        )
    except Exception as e:
        logger.error(f"Error assuming OIDC role: {e}")
        raise e

    creds = resp["Credentials"]
    logger.debug("S3 setup assumed role with credentials")

    return aioboto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )


@asynccontextmanager
async def get_s3_client(
    credentials: S3Credentials | RefreshableS3Credentials | None = None,
) -> AsyncGenerator[S3ServiceResource, object]:
    """Get an async S3 resource client for interacting with S3.

    Accepts a :class:`RefreshableS3Credentials` holder for operations that can
    outlive one STS lease; the resulting client re-resolves credentials per
    request, so rotating the holder takes effect mid-upload.

    Yields:
        Async S3 resource client from aioboto3
    """
    session = _get_s3_session(credentials)
    config = AioConfig(
        signature_version="s3v4",
        read_timeout=60,  # default; explicit so the retry strategy is obvious
        connect_timeout=60,
        # populate downloads up to 100 objects concurrently (the
        # with_concurrency_limit on _download_single_object). botocore's default
        # pool of 10 starves that — the small-file storm (most snapshot files are
        # <1MB) ends up effectively 10-way. Size the pool to the download
        # concurrency so loose-file populate isn't connection-bound, and so the
        # boto3 baseline is a fair comparison against the 256-worker s5cmd path.
        max_pool_connections=256,
        # "legacy" (the botocore default) does NOT retry on ReadTimeoutError.
        # "standard" does, so timed-out multipart chunks are re-fetched
        # individually instead of restarting the entire file download.
        retries={"max_attempts": 5, "mode": "standard"},
    )
    region = credentials.region if credentials else settings.S3_DEFAULT_REGION
    async with session.resource("s3", config=config, region_name=region) as s3:
        yield s3
