"""Session-scoped cache helpers for USPTO status codes."""

from __future__ import annotations

from typing import Any

from loguru import logger
from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError

from mcp_servers.uspto.api import get_uspto_client
from mcp_servers.uspto.auth.keys import APIKeyManager
from mcp_servers.uspto.db import get_db
from mcp_servers.uspto.db.models import StatusCode

_STATUS_CODES_TTL_HOURS = 24


def _cache_cutoff_expression():
    return func.datetime("now", f"-{_STATUS_CODES_TTL_HOURS} hours")


async def _purge_expired_status_codes() -> None:
    try:
        async with get_db() as session:
            await session.execute(
                delete(StatusCode).where(StatusCode.retrieved_at < _cache_cutoff_expression()),
            )
    except (RuntimeError, SQLAlchemyError) as exc:
        logger.warning("Status code cache cleanup skipped", error=str(exc))


async def purge_expired_status_codes() -> None:
    """Remove expired status codes from the session cache."""
    await _purge_expired_status_codes()


def status_codes_cache_cutoff():
    """Return the SQL expression used for status code TTL filtering."""
    return _cache_cutoff_expression()


async def is_status_codes_cached() -> bool:
    """Check if status codes are already cached in this session."""
    try:
        async with get_db() as session:
            result = await session.execute(
                select(func.count())
                .select_from(StatusCode)
                .where(StatusCode.retrieved_at >= _cache_cutoff_expression()),
            )
            return int(result.scalar_one()) > 0
    except (RuntimeError, SQLAlchemyError) as exc:
        logger.warning("Status code cache check skipped", error=str(exc))
        return False


async def fetch_and_cache_status_codes() -> list[dict[str, Any]] | None:
    """Fetch status codes from USPTO API and cache in database."""
    api_key = APIKeyManager.get_api_key_from_context()
    client = get_uspto_client(api_key=api_key)
    try:
        response = await client.get_status_codes()
    finally:
        await client.aclose()

    if "error" in response:
        logger.warning(
            "Status codes fetch failed",
            error=response.get("error"),
        )
        return None

    status_codes = response.get("statusCodes")
    if status_codes is None:
        status_codes = response.get("statusCodeBag", [])
    if not isinstance(status_codes, list):
        logger.warning("Status codes response malformed", status_codes=status_codes)
        return None

    # Get version from top-level response (transformed format) or individual codes (raw format)
    version = response.get("version") or next(
        (
            code.get("version")
            for code in status_codes
            if isinstance(code, dict) and code.get("version")
        ),
        None,
    )
    cached_codes: list[dict[str, Any]] = []

    try:
        async with get_db() as session:
            await session.execute(delete(StatusCode))
            for code in status_codes:
                if not isinstance(code, dict):
                    continue
                status_code = code.get("statusCode") or code.get("applicationStatusCode")
                description = code.get("statusDescriptionText") or code.get(
                    "applicationStatusDescriptionText"
                )
                if not status_code or description is None:
                    continue
                record = StatusCode(
                    status_code=str(status_code),
                    status_description_text=str(description),
                    version=version,  # Use top-level version for all codes
                )
                session.add(record)
                cached_codes.append(code)
    except (RuntimeError, SQLAlchemyError) as exc:
        logger.warning("Status code cache write skipped", error=str(exc))
        return None

    logger.info("Status codes cached", count=len(cached_codes), version=version)
    return cached_codes


async def get_cached_status_codes(allow_stale: bool = False) -> list[dict[str, Any]] | None:
    """Return all cached status codes for the current session."""
    try:
        async with get_db() as session:
            query = select(StatusCode)
            if not allow_stale:
                query = query.where(StatusCode.retrieved_at >= _cache_cutoff_expression())
            result = await session.execute(query)
            rows = list(result.scalars().all())
    except (RuntimeError, SQLAlchemyError) as exc:
        logger.warning("Status code cache read skipped", error=str(exc))
        return None

    if not rows:
        return None

    return [
        {
            "statusCode": row.status_code,
            "statusDescriptionText": row.status_description_text,
            "version": row.version,
        }
        for row in rows
    ]


async def get_status_code_description(status_code: str) -> str | None:
    """Get status code description, fetching from API if not cached."""
    if not await is_status_codes_cached():
        await fetch_and_cache_status_codes()

    try:
        async with get_db() as session:
            result = await session.execute(
                select(StatusCode).where(StatusCode.status_code == status_code),
            )
            row = result.scalar_one_or_none()
    except (RuntimeError, SQLAlchemyError) as exc:
        logger.warning("Status code lookup skipped", error=str(exc))
        return None

    if not row:
        return None
    return row.status_description_text
