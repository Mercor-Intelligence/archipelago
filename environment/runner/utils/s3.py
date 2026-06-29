"""S3 client utilities for interacting with S3-compatible storage.

Credential priority: explicit S3Credentials → OIDC exchange (MODAL_IDENTITY_TOKEN +
MODAL_OIDC_ROLE_ARN) → boto3 default chain (AWS_* env vars, instance metadata, etc.).
"""

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import aioboto3
from aiobotocore.config import AioConfig
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


def _get_s3_session(credentials: S3Credentials | None = None) -> aioboto3.Session:
    """Build an aioboto3 session.

    Priority: explicit credentials → OIDC exchange → default chain (includes AWS_* env vars, local dev).
    """
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
    credentials: S3Credentials | None = None,
) -> AsyncGenerator[S3ServiceResource, object]:
    """Get an async S3 resource client for interacting with S3.

    Yields:
        Async S3 resource client from aioboto3
    """
    session = _get_s3_session(credentials)
    config = AioConfig(
        signature_version="s3v4",
        read_timeout=60,  # default; explicit so the retry strategy is obvious
        connect_timeout=60,
        # "legacy" (the botocore default) does NOT retry on ReadTimeoutError.
        # "standard" does, so timed-out multipart chunks are re-fetched
        # individually instead of restarting the entire file download.
        retries={"max_attempts": 5, "mode": "standard"},
    )
    region = credentials.region if credentials else settings.S3_DEFAULT_REGION
    async with session.resource("s3", config=config, region_name=region) as s3:
        yield s3
