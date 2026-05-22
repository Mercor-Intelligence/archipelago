"""
Mercor document cache extractor.

Routes document parsing through Mercor's document cache service instead of
calling a third-party parser directly. Identical files (by content hash) are
parsed at most once and reused across snapshots and trajectories.

The cache is opaque to the grading service: the caller hands over a file and
gets back text + images + sub-artifacts. Configured via:

- `MERCOR_DOCUMENT_API`     — full URL of the document cache parse endpoint
                              (per-env constant, e.g.
                              https://api.studio.mercor.com/internal/archipelago/document-cache/parse)
- `MERCOR_DOCUMENT_API_KEY` — internal API key used to authenticate

When either is unset the factory falls back to the regular `ReductoExtractor`.

This file contains both the HTTP client and the `BaseFileExtractor` adapter
because they're tightly coupled and small.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from loguru import logger
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from .reducto.types import ReductoExtractedContent
from .reducto_extractor import ReductoExtractor

DEFAULT_REQUEST_TIMEOUT_SEC = 600.0  # parses on miss can take a while
DEFAULT_CONNECT_TIMEOUT_SEC = 30.0


def mercor_document_cache_env_configured() -> bool:
    """True iff MERCOR_DOCUMENT_API and MERCOR_DOCUMENT_API_KEY are both set."""
    return bool(os.getenv("MERCOR_DOCUMENT_API")) and bool(
        os.getenv("MERCOR_DOCUMENT_API_KEY")
    )


def _is_retryable_error(exception: BaseException) -> bool:
    if isinstance(exception, httpx.HTTPStatusError):
        status_code = exception.response.status_code
        return status_code == 429 or status_code >= 500
    if isinstance(exception, httpx.ConnectError | httpx.TimeoutException):
        return True
    return False


@dataclass
class _ClientConfig:
    endpoint_url: str
    api_key: str
    timeout_sec: float = DEFAULT_REQUEST_TIMEOUT_SEC
    connect_timeout_sec: float = DEFAULT_CONNECT_TIMEOUT_SEC


class MercorDocumentCacheClient:
    """HTTP client for the Mercor document cache parse endpoint."""

    def __init__(
        self,
        *,
        endpoint_url: str | None = None,
        api_key: str | None = None,
        timeout_sec: float = DEFAULT_REQUEST_TIMEOUT_SEC,
    ):
        resolved_url = endpoint_url or os.getenv("MERCOR_DOCUMENT_API") or ""
        resolved_key = api_key or os.getenv("MERCOR_DOCUMENT_API_KEY") or ""
        if not resolved_url or not resolved_key:
            raise RuntimeError(
                "MERCOR_DOCUMENT_API and MERCOR_DOCUMENT_API_KEY must both be set "
                "to use the Mercor document cache."
            )
        self._cfg = _ClientConfig(
            endpoint_url=resolved_url,
            api_key=resolved_key,
            timeout_sec=timeout_sec,
        )

    def _headers(self) -> dict[str, str]:
        return {"X-API-Key": self._cfg.api_key}

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=20),
        retry=retry_if_exception(_is_retryable_error),
        reraise=True,
    )
    async def _post_parse(self, file_path: Path) -> dict[str, Any]:
        timeout = httpx.Timeout(
            self._cfg.timeout_sec, connect=self._cfg.connect_timeout_sec
        )
        async with httpx.AsyncClient(timeout=timeout) as client:
            with file_path.open("rb") as f:
                files = {"file": (file_path.name, f)}
                data = {"file_extension": file_path.suffix.lower()}
                resp = await client.post(
                    self._cfg.endpoint_url,
                    files=files,
                    data=data,
                    headers=self._headers(),
                )
        resp.raise_for_status()
        return resp.json()

    async def extract_from_file(
        self,
        file_path: Path,
        *,
        include_images: bool = True,
        sub_artifact_index: int | None = None,
    ) -> ReductoExtractedContent:
        """Drop-in replacement for `ReductoClient.extract_from_file`.

        The cache always returns the full document. When `sub_artifact_index`
        is provided we filter client-side.
        """
        try:
            payload = await self._post_parse(file_path)
        except Exception as e:
            logger.warning(f"[MERCOR DOC CACHE] request failed for {file_path}: {e}")
            raise

        content = payload.get("content") or {}
        text = content.get("text", "") or ""
        images = list(content.get("images") or [])
        sub_artifacts = list(content.get("sub_artifacts") or [])

        if not include_images:
            images = []
            for sa in sub_artifacts:
                sa["images"] = []

        if sub_artifact_index is not None:
            matching = [
                sa for sa in sub_artifacts if sa.get("index") == sub_artifact_index
            ]
            if matching:
                only = matching[0]
                text = only.get("content", "") or ""
                images = list(only.get("images") or []) if include_images else []
                sub_artifacts = [only]
            else:
                logger.debug(
                    f"[MERCOR DOC CACHE] sub_artifact_index={sub_artifact_index} "
                    f"not present in result for {file_path}; returning full document"
                )

        logger.debug(
            f"[MERCOR DOC CACHE] {file_path.name} text_chars={len(text)} "
            f"images={len(images)} sub_artifacts={len(sub_artifacts)}"
        )

        return ReductoExtractedContent(
            text=text,
            images=images,
            sub_artifacts=sub_artifacts,
        )


class MercorDocumentCache(ReductoExtractor):
    """File extractor backed by the Mercor document cache.

    Same supported file types and same `extract_from_file` interface as
    `ReductoExtractor`. Subclasses `ReductoExtractor` only so existing
    `isinstance(x, ReductoExtractor)` checks (in `FileExtractionService`
    and the snapshot diff) keep working — implementation detail, not part
    of the public surface.
    """

    def __init__(  # pyright: ignore[reportMissingSuperCall]
        self,
        *,
        endpoint_url: str | None = None,
        api_key: str | None = None,
    ):
        # Intentionally skip ReductoExtractor.__init__: it requires
        # REDUCTO_API_KEY, but for this subclass all parsing happens behind
        # the Mercor endpoint and no third-party key is needed.
        self.api_key: str | None = None
        self._client: MercorDocumentCacheClient = MercorDocumentCacheClient(
            endpoint_url=endpoint_url, api_key=api_key
        )

    @property
    def name(self) -> str:  # pyright: ignore[reportImplicitOverride]
        return "mercor-document-cache"
