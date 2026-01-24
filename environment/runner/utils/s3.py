"""S3 client utilities for interacting with S3-compatible storage.

This module provides a centralized way to create S3 clients using credentials
and configuration from application settings. Supports both AWS S3 and
S3-compatible APIs.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import aioboto3
from aiobotocore.config import AioConfig
from types_aiobotocore_s3.service_resource import S3ServiceResource

from runner.utils.settings import get_settings

settings = get_settings()


@asynccontextmanager
async def get_s3_client() -> AsyncGenerator[S3ServiceResource, Any]:
    """Get an async S3 resource client for interacting with S3.

    Creates an async S3 resource client using credentials from settings.
    If S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY are set, uses those;
    otherwise falls back to default AWS credential chain (IAM roles, etc.).

    The client is configured with S3v4 signature version and uses the
    region specified in S3_DEFAULT_REGION setting.

    Example usage:
        async with get_s3_client() as s3:
            bucket = await s3.Bucket("mybucket")
            async for s3_object in bucket.objects.all():
                print(s3_object.key)

    Yields:
        Async S3 resource client from aioboto3
    """
    if settings.S3_ACCESS_KEY_ID and settings.S3_SECRET_ACCESS_KEY:
        session = aioboto3.Session(
            aws_access_key_id=settings.S3_ACCESS_KEY_ID,
            aws_secret_access_key=settings.S3_SECRET_ACCESS_KEY,
            aws_session_token=settings.S3_SESSION_TOKEN,
        )
    else:
        session = aioboto3.Session()

    config = AioConfig(signature_version="s3v4")
    async with session.resource(
        "s3", config=config, region_name=settings.S3_DEFAULT_REGION
    ) as s3:
        yield s3
