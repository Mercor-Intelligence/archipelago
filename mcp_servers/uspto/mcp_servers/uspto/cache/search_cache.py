"""Session-scoped search cache helpers for USPTO queries."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from loguru import logger
from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError

from mcp_servers.uspto.db import get_db
from mcp_servers.uspto.db.models import SearchCache

_search_cache_hits = 0
_search_cache_misses = 0
_SEARCH_CACHE_TTL_HOURS = 1


def generate_cache_key(
    query_text: str,
    filters: dict | None,
    *,
    start: int,
    rows: int,
    sort: str | None,
) -> str:
    """Generate deterministic cache key from query + filters + pagination."""
    cache_data = {
        "query": query_text,
        "filters": filters or {},
        "start": start,
        "rows": rows,
        "sort": sort,
    }
    digest = hashlib.sha256(json.dumps(cache_data, sort_keys=True).encode("utf-8")).hexdigest()
    return digest[:16]


def _track_cache_event(hit: bool) -> None:
    global _search_cache_hits, _search_cache_misses
    if hit:
        _search_cache_hits += 1
    else:
        _search_cache_misses += 1


def reset_search_cache_metrics() -> None:
    """Reset cache hit/miss counters for a new session."""
    global _search_cache_hits, _search_cache_misses
    _search_cache_hits = 0
    _search_cache_misses = 0


def _current_hit_rate() -> float:
    total = _search_cache_hits + _search_cache_misses
    if total == 0:
        return 0.0
    return _search_cache_hits / total


def _cache_cutoff_expression():
    return func.datetime("now", f"-{_SEARCH_CACHE_TTL_HOURS} hours")


async def _purge_expired_cache() -> None:
    try:
        async with get_db() as session:
            await session.execute(
                delete(SearchCache).where(SearchCache.cached_at < _cache_cutoff_expression()),
            )
    except (RuntimeError, SQLAlchemyError) as exc:
        logger.warning("Search cache cleanup skipped", error=str(exc))


async def _cache_size() -> int | None:
    try:
        async with get_db() as session:
            result = await session.execute(
                select(func.count())
                .select_from(SearchCache)
                .where(SearchCache.cached_at >= _cache_cutoff_expression()),
            )
            return int(result.scalar_one())
    except (RuntimeError, SQLAlchemyError) as exc:
        logger.warning("Search cache size unavailable", error=str(exc))
        return None


async def get_cached_search(
    query_text: str,
    filters: dict | None,
    *,
    start: int,
    rows: int,
    sort: str | None,
) -> dict | None:
    """Retrieve cached search results if available."""
    await _purge_expired_cache()
    cache_key = generate_cache_key(
        query_text,
        filters,
        start=start,
        rows=rows,
        sort=sort,
    )
    try:
        async with get_db() as session:
            result = await session.execute(
                select(SearchCache).where(
                    SearchCache.id == cache_key,
                    SearchCache.cached_at >= _cache_cutoff_expression(),
                ),
            )
            cache_entry: SearchCache | None = result.scalar_one_or_none()
            if cache_entry is None:
                _track_cache_event(hit=False)
                logger.info("Search cache miss", cache_key=cache_key, query=query_text)
                logger.info(
                    "Search cache metrics",
                    hit_rate=_current_hit_rate(),
                    hits=_search_cache_hits,
                    misses=_search_cache_misses,
                )
                return None

            try:
                parsed_results = json.loads(cache_entry.results)
            except json.JSONDecodeError as exc:
                _track_cache_event(hit=False)
                await session.delete(cache_entry)
                await session.flush()
                logger.warning("Search cache decode failed", cache_key=cache_key, error=str(exc))
                logger.info(
                    "Search cache metrics",
                    hit_rate=_current_hit_rate(),
                    hits=_search_cache_hits,
                    misses=_search_cache_misses,
                )
                return None

            _track_cache_event(hit=True)
            logger.info("Search cache hit", cache_key=cache_key, query=query_text)
            logger.info(
                "Search cache metrics",
                hit_rate=_current_hit_rate(),
                hits=_search_cache_hits,
                misses=_search_cache_misses,
            )
            return {
                "results": parsed_results,
                "totalCount": cache_entry.total_count,
                "fromCache": True,
                "cachedAt": cache_entry.cached_at,
            }
    except (RuntimeError, SQLAlchemyError) as exc:
        logger.warning("Search cache read skipped", error=str(exc))
        return None


async def cache_search_results(
    query_text: str,
    filters: dict | None,
    *,
    start: int,
    rows: int,
    sort: str | None,
    results: list[Any],
    total_count: int | None,
) -> None:
    """Store search results in session-scoped cache."""
    await _purge_expired_cache()
    cache_key = generate_cache_key(
        query_text,
        filters,
        start=start,
        rows=rows,
        sort=sort,
    )
    try:
        async with get_db() as session:
            existing = await session.execute(
                select(SearchCache).where(SearchCache.id == cache_key),
            )
            current = existing.scalar_one_or_none()
            if current:
                await session.delete(current)
                await session.flush()

            record = SearchCache(
                id=cache_key,
                query_text=query_text,
                filters=json.dumps(filters) if filters else None,
                results=json.dumps(results),
                total_count=total_count,
            )
            session.add(record)

        size = await _cache_size()
        logger.info(
            "Search cache stored",
            cache_key=cache_key,
            query=query_text,
            total_count=total_count,
            cache_size=size,
        )
    except (RuntimeError, SQLAlchemyError) as exc:
        logger.warning("Search cache write skipped", error=str(exc))
