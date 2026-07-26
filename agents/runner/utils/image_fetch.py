"""Fetch remote image URLs and re-encode them as data: URLs.

CLI-wrapping agent harnesses (codex_agent, claude_code_agent, gemini_cli_agent,
cursor_agent, …) all share the same problem: the CLI's image-input flag takes a
local file path, but task definitions in Studio commonly embed images as
`image_url` blocks with remote URLs (presigned S3, https). Rather than each
MCP server doing its own outbound HTTP — which would multiply SSRF surface,
egress policy, streaming/timeout handling, and per-protocol error handling —
this helper resolves remote URLs to base64-encoded data URLs on the harness
side. The MCP servers then only need to decode the data URL and write bytes
to disk.

The 5MB-per-image cap matches Codex CLI's documented soft guideline and is
a reasonable upper bound for the model context budget of any vision model.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from io import BytesIO
from typing import Any

import httpx
from loguru import logger
from openai.types.chat.chat_completion_tool_param import ChatCompletionToolParam
from PIL import Image

from runner.agents.models import LitellmAnyMessage, get_msg_attr, get_msg_content

MAX_IMAGE_BYTES = 5 * 1024 * 1024
# Anthropic hard-rejects any image with a dimension over 8000px, regardless
# of how few images the request holds.
MAX_ANTHROPIC_IMAGE_DIMENSION = 8000
MAX_ANTHROPIC_MANY_IMAGE_DIMENSION = 2000
MAX_ANTHROPIC_REQUEST_BYTES = 32 * 1024 * 1024
MAX_ANTHROPIC_IMAGES_PER_REQUEST = 600
MIN_ANTHROPIC_IMAGES_FOR_DOWNSCALE = 20
# Rough JSON wrapper size for an image_url block excluding the base64 payload.
_IMAGE_URL_BLOCK_OVERHEAD_BYTES = 80
FETCH_TIMEOUT_SECONDS = 30.0
# httpx.AsyncClient's default is 20; we want enough hops to traverse
# legitimate CDN/presigned-URL chains but not unbounded.
MAX_REDIRECTS = 10

# Allowed schemes for inline pass-through (no fetch needed).
_DATA_SCHEMES = ("data:",)
# Schemes the helper will resolve to bytes via outbound HTTP.
_HTTPS_SCHEMES = ("https://",)


class ImageFetchError(Exception):
    """Raised when an image URL cannot be resolved within budget."""


class AnthropicRequestImageBudgetError(Exception):
    """Raised when Anthropic request image limits cannot be satisfied by resize."""

    image_count: int
    max_count: int

    def __init__(self, image_count: int, max_count: int) -> None:
        self.image_count = image_count
        self.max_count = max_count
        super().__init__(
            f"Anthropic image count exceeds limit: {image_count} images (limit: {max_count})"
        )


def _sniff_image_mime(data: bytes) -> str | None:
    """Identify a known image format from the leading bytes.

    Returns the MIME type ("image/png", "image/jpeg", "image/gif",
    "image/webp") if the magic bytes match a supported format, else None.

    Used as a fallback when the upstream response advertises a generic
    content-type (e.g. S3 stores objects as `binary/octet-stream` when no
    ContentType was set on upload — see
    rl-studio/server/packages/custom_field_files/service.py upload_files()).
    Without this, every downstream MCP server's `_extension_for_mime` maps
    the wrong MIME to `.bin`, and the resulting file is rejected by Gemini
    CLI's `@path` resolver (extension-based via mime/lite) and by Studio's
    `mcp__gateway__filesystem_read_image_file` (extension allowlist).
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    # WEBP = RIFF<size>WEBP; need 12 bytes to confirm.
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


async def _fetch_streaming(
    client: httpx.AsyncClient, url: str
) -> tuple[bytes, str | None]:
    """GET `url`, following redirects manually with per-hop https validation.

    `client.stream("GET", ...)` is invoked with redirects disabled at the
    client level (see resolve_to_data_urls). We follow them ourselves so we
    can reject any hop that tries to land on a non-https URL — otherwise an
    https→http://internal-host redirect would silently bypass the SSRF
    mitigation and the docstring's https-only guarantee.

    Body is streamed with an incremental byte counter; the connection is
    aborted past MAX_IMAGE_BYTES so a hostile or misconfigured source can't
    blow memory.
    """
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        if not current.startswith(_HTTPS_SCHEMES):
            raise ImageFetchError(
                f"Refusing to follow redirect to non-https URL: {current[:80]}"
            )
        async with client.stream(
            "GET", current, timeout=FETCH_TIMEOUT_SECONDS
        ) as response:
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise ImageFetchError(
                        f"Redirect from {current[:80]} has no Location header"
                    )
                # Resolve relative locations against the current URL.
                current = str(httpx.URL(current).join(location))
                continue

            response.raise_for_status()
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > MAX_IMAGE_BYTES:
                    raise ImageFetchError(
                        f"Image exceeds {MAX_IMAGE_BYTES}-byte cap during fetch from {url[:80]}"
                    )
                chunks.append(chunk)
            raw = b"".join(chunks)
            mime = (
                response.headers.get("content-type", "").split(";")[0].strip() or None
            )
            # If the response advertised a generic MIME (e.g. S3's
            # binary/octet-stream default when no ContentType was set on
            # upload), sniff magic bytes for a real image format. Only
            # override when sniff is confident — leave non-image bytes
            # alone so we don't mislabel something.
            if not mime or not mime.startswith("image/"):
                sniffed = _sniff_image_mime(raw[:16])
                if sniffed is not None:
                    mime = sniffed
            return raw, mime

    raise ImageFetchError(
        f"Too many redirects (>{MAX_REDIRECTS}) starting from {url[:80]}"
    )


def _b64_len(raw: bytes) -> int:
    """Length of standard base64 encoding for `raw` (no actual encode)."""
    n = len(raw)
    return 0 if n == 0 else 4 * ((n + 2) // 3)


def _encode_image_under_budget(
    img: Image.Image, *, max_b64_bytes: int = MAX_IMAGE_BYTES
) -> tuple[bytes, str]:
    """Re-encode an image so its base64 representation is at most max_b64_bytes."""
    working = img
    if working.mode not in ("RGB", "L"):
        if working.mode in ("RGBA", "LA") or (
            working.mode == "P" and "transparency" in working.info
        ):
            buf = BytesIO()
            working.save(buf, format="PNG", optimize=True)
            raw = buf.getvalue()
            if _b64_len(raw) <= max_b64_bytes:
                return raw, "image/png"
        working = working.convert("RGB")

    for quality in (85, 70, 50, 30):
        buf = BytesIO()
        working.save(buf, format="JPEG", quality=quality, optimize=True)
        raw = buf.getvalue()
        if _b64_len(raw) <= max_b64_bytes:
            return raw, "image/jpeg"

    w, h = working.size
    while max(w, h) > 256:
        w = max(1, w * 3 // 4)
        h = max(1, h * 3 // 4)
        resized = working.resize((w, h), Image.Resampling.LANCZOS)
        buf = BytesIO()
        resized.save(buf, format="JPEG", quality=30, optimize=True)
        raw = buf.getvalue()
        if _b64_len(raw) <= max_b64_bytes:
            return raw, "image/jpeg"
        working = resized

    buf = BytesIO()
    working.save(buf, format="JPEG", quality=30, optimize=True)
    return buf.getvalue(), "image/jpeg"


def normalize_mcp_image_for_anthropic(
    data_b64: str,
    mime_type: str,
    *,
    downscale: bool,
    max_b64_bytes: int | None = None,
) -> tuple[str, str]:
    """Resize/compress MCP tool images for Anthropic tool-result embedding."""
    limit = max_b64_bytes if max_b64_bytes is not None else MAX_IMAGE_BYTES
    try:
        raw = base64.b64decode(data_b64, validate=True)
    except Exception:
        return data_b64, mime_type

    try:
        img = Image.open(BytesIO(raw))
        img.load()
    except Exception:
        return data_b64, mime_type

    max_dim = (
        MAX_ANTHROPIC_MANY_IMAGE_DIMENSION
        if downscale
        else MAX_ANTHROPIC_IMAGE_DIMENSION
    )
    changed = False
    if max(img.size) > max_dim:
        img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
        changed = True

    if len(data_b64) <= limit and not changed:
        return data_b64, mime_type

    encoded, mime = _encode_image_under_budget(img, max_b64_bytes=limit)
    return base64.b64encode(encoded).decode("ascii"), mime


@dataclass
class _ImageSlot:
    """Mutable image_url block reference inside a copied message list."""

    block: dict[str, Any]


def _count_image_url_blocks(messages: list[LitellmAnyMessage]) -> int:
    total = 0
    for msg in messages:
        content = get_msg_content(msg)
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "image_url":
                    total += 1
    return total


def _parse_data_uri(url: str) -> tuple[str, str] | None:
    if not url.startswith("data:") or ";base64," not in url:
        return None
    header, payload = url.split(";base64,", 1)
    mime = header[5:] or "application/octet-stream"
    return mime, payload


def _copy_messages_and_collect_data_uri_slots(
    messages: list[LitellmAnyMessage],
) -> tuple[list[LitellmAnyMessage], list[_ImageSlot]]:
    from litellm.types.utils import Message

    copied: list[LitellmAnyMessage] = []
    slots: list[_ImageSlot] = []

    for msg in messages:
        if isinstance(msg, dict):
            msg_copy: dict[str, Any] = dict(msg)
            content = msg.get("content")
            if isinstance(content, list):
                new_content: list[Any] = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "image_url":
                        new_block = dict(block)
                        image_url = new_block.get("image_url")
                        if isinstance(image_url, dict):
                            new_block["image_url"] = dict(image_url)
                            url = new_block["image_url"].get("url")
                            if isinstance(url, str) and _parse_data_uri(url):
                                slots.append(_ImageSlot(block=new_block))
                        new_content.append(new_block)
                    else:
                        new_content.append(block)
                msg_copy["content"] = new_content
            copied.append(msg_copy)  # pyright: ignore[reportArgumentType]
            continue

        if isinstance(msg, Message):
            pydantic_copy = msg.model_copy(deep=True)
            content = pydantic_copy.content
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "image_url":
                        continue
                    image_url = block.get("image_url")
                    if not isinstance(image_url, dict):
                        continue
                    url = image_url.get("url")
                    if isinstance(url, str) and _parse_data_uri(url):
                        slots.append(_ImageSlot(block=block))
            copied.append(pydantic_copy)
            continue

        copied.append(msg)

    return copied, slots


def _estimate_tool_calls_bytes(tool_calls: Any) -> int:
    """Serialized size of assistant tool_calls (tool_use blocks in the API body)."""
    if not tool_calls:
        return 0
    total = 0
    for tc in tool_calls:
        if isinstance(tc, dict):
            total += len(json.dumps(tc, default=str).encode())
            continue
        if hasattr(tc, "function"):
            tc_id = getattr(tc, "id", "") or ""
            fn = getattr(tc, "function", None)
            name = getattr(fn, "name", "") if fn else ""
            raw_args = getattr(fn, "arguments", "") if fn else ""
            total += len(str(tc_id).encode())
            total += len(str(name).encode())
            if isinstance(raw_args, str):
                total += len(raw_args.encode())
            elif raw_args:
                total += len(json.dumps(raw_args, default=str).encode())
            continue
        total += len(str(tc).encode())
    return total


def _estimate_non_image_bytes(
    messages: list[LitellmAnyMessage],
    tools: list[ChatCompletionToolParam] | None,
) -> int:
    total = 0
    for msg in messages:
        for key in ("role", "name", "tool_call_id"):
            val = get_msg_attr(msg, key)
            if isinstance(val, str):
                total += len(val.encode())
        total += _estimate_tool_calls_bytes(get_msg_attr(msg, "tool_calls"))
        content = get_msg_content(msg)
        if isinstance(content, str):
            total += len(content.encode())
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "image_url":
                    total += _IMAGE_URL_BLOCK_OVERHEAD_BYTES
                elif block_type == "text":
                    text = block.get("text", "")
                    if isinstance(text, str):
                        total += len(text.encode())
                else:
                    total += len(json.dumps(block, default=str).encode())
    if tools:
        total += len(json.dumps(tools, default=str).encode())
    return total


def apply_anthropic_image_policy(
    messages: list[LitellmAnyMessage],
    tools: list[ChatCompletionToolParam] | None,
    *,
    model: str,
) -> list[LitellmAnyMessage]:
    """Sanitize inline images in conversation history for Anthropic API limits."""
    if not model.startswith("anthropic/"):
        return messages

    image_count = _count_image_url_blocks(messages)
    if image_count > MAX_ANTHROPIC_IMAGES_PER_REQUEST:
        raise AnthropicRequestImageBudgetError(
            image_count, MAX_ANTHROPIC_IMAGES_PER_REQUEST
        )

    if image_count == 0:
        return messages

    copied, slots = _copy_messages_and_collect_data_uri_slots(messages)
    if not slots:
        return copied

    downscale_all = image_count >= MIN_ANTHROPIC_IMAGES_FOR_DOWNSCALE
    text_bytes = _estimate_non_image_bytes(messages, tools)
    remaining = max(0, MAX_ANTHROPIC_REQUEST_BYTES - text_bytes)
    per_image_b64 = min(MAX_IMAGE_BYTES, remaining // max(1, len(slots)))

    for slot in slots:
        image_url = slot.block.get("image_url")
        if not isinstance(image_url, dict):
            continue
        url = image_url.get("url")
        if not isinstance(url, str):
            continue
        parsed = _parse_data_uri(url)
        if parsed is None:
            logger.debug("apply_anthropic_image_policy: skipping non-data image_url")
            continue
        mime, data_b64 = parsed
        out_b64, out_mime = normalize_mcp_image_for_anthropic(
            data_b64,
            mime,
            downscale=downscale_all,
            max_b64_bytes=per_image_b64,
        )
        image_url["url"] = f"data:{out_mime};base64,{out_b64}"

    return copied


def _to_data_url(raw: bytes, mime: str | None) -> str:
    mime = mime or "application/octet-stream"
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{encoded}"


async def resolve_to_data_urls(
    images: list[dict[str, Any]],
    *,
    client: httpx.AsyncClient | None = None,
) -> list[dict[str, Any]]:
    """Rewrite every entry in `images` so its `url` is a `data:` URL.

    - `data:...` entries pass through unchanged.
    - `https://...` entries are fetched (streamed, size-capped) and re-encoded.
    - Other schemes (http, s3, file, …) are rejected with ImageFetchError so the
      caller sees a clear contract violation rather than a silent CLI failure
      downstream.

    Each entry's other keys (e.g. `detail`) are preserved.
    """
    if not images:
        return []

    owns_client = client is None
    if client is None:
        # follow_redirects=False so _fetch_streaming can validate the scheme
        # at each hop and refuse https → http redirects (SSRF guard).
        client = httpx.AsyncClient(follow_redirects=False)

    resolved: list[dict[str, Any]] = []
    try:
        for i, image in enumerate(images):
            url = image.get("url")
            if not isinstance(url, str) or not url:
                raise ImageFetchError(f"Image {i} has no url")

            if url.startswith(_DATA_SCHEMES):
                resolved.append(dict(image))
                continue

            if url.startswith(_HTTPS_SCHEMES):
                try:
                    raw, mime = await _fetch_streaming(client, url)
                except (httpx.HTTPError, httpx.InvalidURL) as exc:
                    raise ImageFetchError(
                        f"Failed to fetch image {i} from {url[:80]}: {exc}"
                    ) from exc
                logger.info(
                    f"image_fetch: resolved {url[:80]} -> data url ({len(raw)} bytes, mime={mime})"
                )
                new_entry = dict(image)
                new_entry["url"] = _to_data_url(raw, mime)
                resolved.append(new_entry)
                continue

            raise ImageFetchError(
                f"Unsupported image URL scheme for image {i}: "
                f"{url[:40]}... (expected data: or https:)"
            )
    finally:
        if owns_client:
            await client.aclose()

    return resolved
