"""S3 client utilities for interacting with S3-compatible storage.

This module provides a centralized way to create S3 clients using Modal OIDC
credentials. Sandboxes receive MODAL_IDENTITY_TOKEN via
include_oidc_identity_token=True and exchange it for short-lived AWS
credentials via STS AssumeRoleWithWebIdentity.
"""

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import aioboto3
from aiobotocore.config import AioConfig
from types_aiobotocore_s3.service_resource import S3ServiceResource

from runner.utils.settings import get_settings

settings = get_settings()


def _get_s3_session() -> aioboto3.Session:
    """Build an aioboto3 session using Modal OIDC credentials.

    Sandboxes receive MODAL_IDENTITY_TOKEN via include_oidc_identity_token=True
    (Modal >= 1.4.0). MODAL_OIDC_ROLE_ARN is set in the Modal secret.
    Falls back to the default credential chain for local dev/testing.
    """
    oidc_token = os.environ.get("MODAL_IDENTITY_TOKEN")
    role_arn = os.environ.get("MODAL_OIDC_ROLE_ARN")

    if not oidc_token or not role_arn:
        if os.environ.get("MODAL_IS_REMOTE"):
            raise RuntimeError(
                "Running on Modal but MODAL_IDENTITY_TOKEN or MODAL_OIDC_ROLE_ARN is not set. "
                "Ensure include_oidc_identity_token=True is passed to Sandbox.create "
                "and MODAL_OIDC_ROLE_ARN is in the Modal secret."
            )
        return aioboto3.Session()

    import boto3

    sts = boto3.client("sts", region_name=settings.S3_DEFAULT_REGION)
    resp = sts.assume_role_with_web_identity(  # pyright: ignore[reportAttributeAccessIssue]
        RoleArn=role_arn,
        RoleSessionName="modal-oidc-environment",
        WebIdentityToken=oidc_token,
    )
    creds = resp["Credentials"]
    return aioboto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )


@asynccontextmanager
async def get_s3_client() -> AsyncGenerator[S3ServiceResource, Any]:
    """Get an async S3 resource client for interacting with S3.

    Yields:
        Async S3 resource client from aioboto3
    """
    session = _get_s3_session()
    config = AioConfig(signature_version="s3v4")
    async with session.resource(
        "s3", config=config, region_name=settings.S3_DEFAULT_REGION
    ) as s3:
        yield s3
