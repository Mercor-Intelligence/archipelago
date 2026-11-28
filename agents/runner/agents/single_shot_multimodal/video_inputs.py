"""Turn task videos into Gemini Files API uploads before a Chat Completions call.

A task's ``video_path`` prompt block reaches the agent as a Chat Completions
``{"type": "video_url", "video_url": {"url": <presigned S3 URL>}}`` block
(see ``rl-studio/server/models/msg_representations.py``). litellm's Gemini and
Vertex transforms have no ``video_url`` case, so the block was dropped without
an error: the run completed and the model answered without ever seeing the
video (DEP-3554).

For Gemini providers this module downloads each video, uploads it through the
Gemini Files API (the same ``_upload_file_with_retry`` path the Gemini-native
agent uses for files and images) and swaps the block for a ``file`` reference,
which Gemini reads. Other providers are returned untouched; ``video_url`` is
still what the AFM provider expects.

Gemini processes an uploaded video asynchronously and rejects it until its state
is ACTIVE, so each Google AI Studio upload is polled until it is ready. Vertex
uploads land in GCS and are usable at once.

No video is ever skipped, because a run without its video is worse than a
failed run. Bad input (no URL, a URL that is not presigned S3, a 4xx download, a
video over 2 GB, a type Gemini refuses, a file
Gemini fails to process) raises ``VideoInputError`` and fails the run.
Transient trouble (connection errors, 5xx, upload errors from litellm, a video
still processing at the deadline) keeps an exception type that
``is_system_error`` treats as retryable, so the run is marked ERROR, not FAILED.

The LLM Gateway has no Files API route (uploads 404), so when a run is
gateway-routed the upload, status checks and cleanup go straight to the
LiteLLM proxy. The model call itself still goes through the gateway.
"""

import asyncio
import base64
import mimetypes
import time
from typing import Any, cast
from urllib.parse import quote, unquote, urlparse

import httpx
from litellm import Timeout, afile_retrieve
from litellm.files.main import afile_delete
from loguru import logger

from runner.agents.responses_agent_v2.utils import (
    _GEMINI_PROVIDERS,
    _GEMINI_SUPPORTED_MIME_TYPES,
    _extract_provider,
    _trajectory_llm_target_kwargs,
    _upload_file_with_retry,
)
from runner.utils.llm_route import files_api_route

# Presigned S3 GETs of task videos; a 10-minute video can be a few hundred MB.
VIDEO_DOWNLOAD_TIMEOUT_SECONDS = 600
DEFAULT_VIDEO_MIME_TYPE = "video/mp4"
# Gemini's Files API rejects files over 2 GB, so a bigger download is wasted;
# the cap also bounds worker memory.
MAX_VIDEO_BYTES = 2 * 1024**3
# Gemini usually finishes processing a few minutes of video within seconds to
# a minute; the deadline leaves room for long, high-resolution uploads.
PROCESSING_DEADLINE_SECONDS = 600
PROCESSING_POLL_START_SECONDS = 2.0
PROCESSING_POLL_MAX_SECONDS = 10.0
PROCESSING_RETRIEVE_TIMEOUT_SECONDS = 30
# Google AI Studio processes videos before use; Vertex (GCS) does not.
_PROVIDERS_NEEDING_ACTIVE_STATE = {"gemini"}


class VideoInputError(RuntimeError):
    """A task video could not be prepared for the model."""


def _filename_from_url(url: str) -> str | None:
    """Original filename from a Studio presigned URL, without the ``file_<id>_`` prefix."""
    if not url.startswith(("http://", "https://")):
        return None
    name = urlparse(url).path.rsplit("/", 1)[-1]
    if name.startswith("file_") and "_" in name[5:]:
        name = name.split("_", 2)[-1]
    return unquote(name) or None


def _video_mime_type(declared: str | None, response_type: str | None, url: str) -> str:
    """Pick the video MIME type: declared format, then a video Content-Type, then the URL."""
    if declared:
        return declared if "/" in declared else f"video/{declared}"
    if response_type and response_type.split(";")[0].startswith("video/"):
        return response_type.split(";")[0]
    guessed, _ = mimetypes.guess_type(urlparse(url).path)
    if guessed and guessed.startswith("video/"):
        return guessed
    return DEFAULT_VIDEO_MIME_TYPE


def _is_s3_host(host: str) -> bool:
    """True for the hosts S3 presigned URLs use (virtual-hosted or path style)."""
    if not host.endswith(".amazonaws.com"):
        return False
    labels = host.split(".")
    return any(label == "s3" or label.startswith("s3-") for label in labels)


def _check_video_url(url: str) -> None:
    """Only fetch presigned S3 URLs, the one shape Studio produces for videos.

    Anything else (another host, plain http, an internal address) is refused
    so a crafted ``video_url`` block cannot make the worker fetch it.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not _is_s3_host(host):
        raise VideoInputError(
            f"Task video URL must be a presigned S3 https URL (got host {host!r})"
        )


async def _fetch_video(url: str, declared_format: str | None) -> tuple[bytes, str]:
    if url.startswith("data:"):
        header, _, payload = url.partition(",")
        header_type = header.split(";")[0].removeprefix("data:") or None
        return base64.b64decode(payload), _video_mime_type(
            declared_format, header_type, ""
        )
    _check_video_url(url)
    async with (
        httpx.AsyncClient(
            timeout=VIDEO_DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=False
        ) as client,
        client.stream("GET", url) as resp,
    ):
        if resp.status_code < 500 and not resp.is_success:
            raise VideoInputError(
                f"Could not download task video: HTTP {resp.status_code}"
            )
        # 5xx raises httpx.HTTPStatusError and transport failures raise
        # httpx.TransportError; both stay retryable for is_system_error.
        resp.raise_for_status()
        declared_size = resp.headers.get("content-length", "")
        if declared_size.isdigit() and int(declared_size) > MAX_VIDEO_BYTES:
            raise VideoInputError(
                f"Task video is {int(declared_size)} bytes; Gemini accepts at most "
                f"{MAX_VIDEO_BYTES}"
            )
        body = bytearray()
        async for chunk in resp.aiter_bytes():
            body.extend(chunk)
            if len(body) > MAX_VIDEO_BYTES:
                raise VideoInputError(
                    f"Task video is over the {MAX_VIDEO_BYTES}-byte Gemini limit"
                )
        content_type = resp.headers.get("content-type")
    return bytes(body), _video_mime_type(declared_format, content_type, url)


def _files_target_kwargs() -> dict[str, Any]:
    """litellm kwargs for task-video Files API calls (see ``files_api_route``)."""
    route = files_api_route(**_trajectory_llm_target_kwargs())
    if route is None:
        return {}
    kwargs: dict[str, Any] = {"api_base": route.base_url, "api_key": route.api_key}
    if route.headers:
        kwargs["extra_headers"] = dict(route.headers)
    return kwargs


async def delete_video_uploads(file_ids: list[str], model: str) -> None:
    """Best-effort deletion of uploaded task videos, via the upload's target."""
    if not file_ids:
        return
    provider = _extract_provider(model)
    if provider not in _GEMINI_PROVIDERS:
        return
    target = _files_target_kwargs()
    for file_id in file_ids:
        try:
            await _delete_file(file_id, provider, target)
            logger.info(f"Deleted uploaded task video {file_id}")
        except Exception as e:
            # Cleanup must never replace the run's real outcome.
            logger.warning(f"Failed to delete uploaded task video {file_id}: {e!r}")


async def _delete_file(file_id: str, provider: str, target: dict[str, Any]) -> None:
    """Delete one upload.

    Through the proxy this is a direct DELETE with ``custom_llm_provider`` as a
    query parameter, for the same reason as ``_retrieve_file_state``: litellm
    1.84's OpenAI-route ``afile_delete`` drops ``extra_body``, so the proxy
    would try to delete the Gemini file on OpenAI.
    """
    if not target.get("api_base"):
        await afile_delete(file_id=file_id, custom_llm_provider=cast(Any, provider))
        return
    async with httpx.AsyncClient(timeout=PROCESSING_RETRIEVE_TIMEOUT_SECONDS) as client:
        resp = await client.delete(
            _proxy_files_url(str(target["api_base"]), file_id),
            params={"custom_llm_provider": provider},
            headers=_proxy_headers(target),
        )
    resp.raise_for_status()


def _proxy_headers(target: dict[str, Any]) -> dict[str, str]:
    headers = dict(target.get("extra_headers") or {})
    headers["Authorization"] = f"Bearer {target.get('api_key', '')}"
    return headers


def _proxy_files_url(api_base: str, file_id: str) -> str:
    """Proxy URL for one uploaded file.

    Gemini and Vertex file ids contain slashes (``files/abc``, ``gs://...``).
    The proxy unescapes ``%2F`` before routing, so a singly-encoded id no
    longer matches ``/v1/files/{file_id}`` and lands on an admin-only route.
    Encoding twice leaves exactly one escaped segment after that unescape
    (checked against the dev proxy for GET and DELETE); ids without slashes
    are unaffected.
    """
    base = api_base.rstrip("/")
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    return f"{base}/files/{quote(quote(file_id, safe=''), safe='')}"


async def _retrieve_file_state(
    file_id: str, provider: str, use_proxy: bool, target_kwargs: dict[str, Any]
) -> tuple[str | None, Any]:
    """Return ``(status, status_details)`` for an uploaded file.

    Through the proxy this is a direct GET with ``custom_llm_provider`` as a
    query parameter: litellm 1.84's OpenAI-route ``afile_retrieve`` drops
    ``extra_body`` and ``extra_headers``, so the proxy would look the Gemini
    file up on OpenAI instead.
    """
    if not use_proxy:
        info = await afile_retrieve(
            file_id=file_id, custom_llm_provider=cast(Any, provider)
        )
        return (
            cast(str | None, getattr(info, "status", None)),
            getattr(info, "status_details", None),
        )

    async with httpx.AsyncClient(timeout=PROCESSING_RETRIEVE_TIMEOUT_SECONDS) as client:
        resp = await client.get(
            _proxy_files_url(str(target_kwargs["api_base"]), file_id),
            params={"custom_llm_provider": provider},
            headers=_proxy_headers(target_kwargs),
        )
    if resp.status_code == 429:
        # Rate-limited, not a bad file: report no state so the caller backs
        # off and polls again (ending in a retryable Timeout at the deadline).
        logger.warning(f"Rate-limited checking task video {file_id}; will retry")
        return None, None
    # 5xx / transport errors stay retryable httpx errors; other 4xx means the
    # file or route is wrong, which retrying will not fix.
    resp.raise_for_status()
    body = cast(dict[str, Any], resp.json())
    return cast(str | None, body.get("status")), body.get("status_details")


async def _wait_until_active(
    file_id: str, provider: str, use_proxy: bool, target_kwargs: dict[str, Any]
) -> None:
    """Poll a Google AI Studio upload until Gemini reports it ACTIVE."""
    if provider not in _PROVIDERS_NEEDING_ACTIVE_STATE:
        return
    deadline = time.monotonic() + PROCESSING_DEADLINE_SECONDS
    delay = PROCESSING_POLL_START_SECONDS
    while True:
        status, details = await _retrieve_file_state(
            file_id, provider, use_proxy, target_kwargs
        )
        if status == "processed":
            return
        if status == "error":
            raise VideoInputError(f"Gemini could not process the task video: {details}")
        if time.monotonic() >= deadline:
            raise Timeout(
                message=(
                    f"Task video {file_id} still processing after "
                    f"{PROCESSING_DEADLINE_SECONDS}s"
                ),
                model="",
                llm_provider=provider,
            )
        await asyncio.sleep(delay)
        delay = min(delay * 2, PROCESSING_POLL_MAX_SECONDS)


async def prepare_video_inputs(
    messages: list[dict[str, Any]], model: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """Replace ``video_url`` blocks with Gemini Files API ``file`` blocks.

    Returns the (possibly rewritten) messages and the uploaded file IDs, which
    the caller must pass to ``delete_video_uploads`` once the model call ends.
    Non-Gemini models and messages without video come back unchanged.
    """
    provider = _extract_provider(model)
    if provider not in _GEMINI_PROVIDERS:
        return messages, []

    has_video = any(
        isinstance(block, dict) and block.get("type") == "video_url"
        for msg in messages
        if isinstance(msg.get("content"), list)
        for block in msg["content"]
    )
    if not has_video:
        return messages, []

    upload_kwargs = _files_target_kwargs()
    use_proxy = bool(upload_kwargs.get("api_base"))

    uploaded_ids: list[str] = []
    result: list[dict[str, Any]] = []
    try:
        for msg in messages:
            content = msg.get("content")
            if not isinstance(content, list):
                result.append(msg)
                continue
            new_content: list[Any] = []
            for block in content:
                if not (isinstance(block, dict) and block.get("type") == "video_url"):
                    new_content.append(block)
                    continue
                video = block.get("video_url")
                url = video if isinstance(video, str) else (video or {}).get("url", "")
                declared = (
                    None if isinstance(video, str) else (video or {}).get("format")
                )
                if not url:
                    raise VideoInputError("video_url block has no URL")

                raw_bytes, mime_type = await _fetch_video(url, declared)
                if mime_type not in _GEMINI_SUPPORTED_MIME_TYPES:
                    raise VideoInputError(
                        f"Gemini does not accept video type {mime_type}"
                    )
                try:
                    uploaded = await _upload_file_with_retry(
                        raw_bytes=raw_bytes,
                        mime_type=mime_type,
                        provider=provider,
                        use_proxy=use_proxy,
                        original_filename=_filename_from_url(url),
                        **upload_kwargs,
                    )
                except Exception as e:
                    # Keep litellm's exception type so is_system_error can
                    # tell a transient upload failure from a bad request.
                    logger.error(f"Could not upload task video to {provider}: {e!r}")
                    raise
                # Registered before polling so a processing failure still
                # deletes the upload.
                uploaded_ids.append(uploaded.id)
                logger.info(
                    f"Uploaded task video ({len(raw_bytes)} bytes, {mime_type}) "
                    f"via the {provider} Files API"
                )
                await _wait_until_active(
                    uploaded.id, provider, use_proxy, upload_kwargs
                )
                new_content.append(
                    {
                        "type": "file",
                        "file": {"file_id": uploaded.id, "format": mime_type},
                    }
                )
            result.append({**msg, "content": new_content})
    except Exception:
        await delete_video_uploads(uploaded_ids, model)
        raise

    return result, uploaded_ids
