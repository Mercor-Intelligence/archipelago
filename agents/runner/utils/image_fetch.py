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

import asyncio
import base64
import json
from collections.abc import Callable
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
# Some non-Anthropic multimodal endpoints enforce a small per-asset size cap and
# hard-400 ("Asset is too large") when a tool-result image exceeds it. The lowest
# known cap is 2 MiB (2 * 1024**2 = 2097152 bytes, tml/Inkling). The endpoint
# error is ambiguous about whether it measures the decoded bytes or the base64
# payload, so we cap the *base64* length below 2 MiB with margin: base64 < ~1.9
# MiB guarantees the decoded image is well under 2 MiB too, clearing the cap
# under either interpretation.
MAX_NON_ANTHROPIC_IMAGE_B64_BYTES = 1_900_000
# Base64 caps keyed by the hosting-provider *segment* of the model string. The
# per-asset cap is a property of the serving host, not the model, so it's keyed
# by host and matched against any "/"-delimited segment — Studio assembles model
# names as "{provider}/{model}" where the model part is itself slash-delimited,
# so the same Tinker deployment appears as "tml/Inkling" (provider=tml) but also
# "openai/tml/Inkling" when an OpenAI-compatible transport fronts it. A prefix
# match would miss the latter. ONLY hosts known to enforce a small per-asset
# limit are listed; every other non-Anthropic provider (OpenAI ~20 MB, Gemini, …)
# keeps the raw pass-through, so we never recompress — and thereby degrade —
# images for models that don't need it. 1:many map (not a per-model literal);
# extend it when a new host starts 400ing on asset size. Overridable per call via
# content_blocks_to_messages(max_image_bytes=).
NON_ANTHROPIC_IMAGE_B64_CAPS: dict[str, int] = {
    "tml": MAX_NON_ANTHROPIC_IMAGE_B64_BYTES,  # Tinker-hosted VLMs (Inkling, …)
}
# A separate class of limit from the caps above: those bound what WE may inline,
# this bounds what the PROVIDER will fetch on our behalf. Vertex resolves an
# `image_url` server-side and 400s past 15 MiB on bytes, never pixels. Keyed by
# provider segment like the map above (RLS-10563).
VERTEX_REMOTE_FETCH_CAP_BYTES = 15_728_640
REMOTE_IMAGE_FETCH_CAPS: dict[str, int] = {
    "vertex_ai": VERTEX_REMOTE_FETCH_CAP_BYTES,
    "vertex_ai_beta": VERTEX_REMOTE_FETCH_CAP_BYTES,
    "gemini": VERTEX_REMOTE_FETCH_CAP_BYTES,
}
# Budget for the re-encoded copy we inline once an image is over the cap. A
# PER-IMAGE CEILING, not a target: the worst real offender re-encodes to 0.43 MiB.
# Loose enough that the encoder never reaches its downscale ladder, so full pixel
# resolution survives — the point for a coordinate-grounding benchmark. Well inside
# what the endpoint accepts inline, which is far looser than its URL-fetch cap.
REMOTE_IMAGE_REENCODE_B64_BUDGET = 7 * 1024 * 1024
# Aggregate ceiling across every image inlined into ONE request, so N over-cap
# images in one history cannot multiply the per-image budget. Worth guarding: a
# "request payload size exceeds" 400 carries neither "context" nor "token", so
# `is_system_error` returns True and a deterministic 400 gets retried.
REMOTE_IMAGE_REQUEST_B64_BUDGET = 28 * 1024 * 1024
# Absolute ceiling on what this policy pulls into container memory. The probe
# reports the size first, so an object past this is skipped, never streamed.
MAX_REMOTE_IMAGE_FETCH_BYTES = 64 * 1024 * 1024
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
    client: httpx.AsyncClient, url: str, *, max_bytes: int = MAX_IMAGE_BYTES
) -> tuple[bytes, str | None]:
    """GET `url`, following redirects manually with per-hop https validation.

    `client.stream("GET", ...)` is invoked with redirects disabled at the
    client level (see resolve_to_data_urls). We follow them ourselves so we
    can reject any hop that tries to land on a non-https URL — otherwise an
    https→http://internal-host redirect would silently bypass the SSRF
    mitigation and the docstring's https-only guarantee.

    Body is streamed with an incremental byte counter; the connection is
    aborted past ``max_bytes`` so a hostile or misconfigured source can't
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
                if total > max_bytes:
                    raise ImageFetchError(
                        f"Image exceeds {max_bytes}-byte cap during fetch from {url[:80]}"
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


def normalize_mcp_image(
    data_b64: str,
    mime_type: str,
    *,
    max_b64_bytes: int,
    max_dim: int | None = None,
) -> tuple[str, str]:
    """Resize/compress an MCP tool image so its base64 payload fits a byte budget.

    Provider-agnostic core shared by the Anthropic and non-Anthropic tool-result
    paths. Decodes the image, optionally thumbnails it to ``max_dim`` (for
    endpoints that also reject oversized *dimensions*, e.g. Anthropic's 8000px
    hard limit — pass ``None`` to skip dimension clamping), then, if the payload
    is still over ``max_b64_bytes`` or was resized, re-encodes it under the byte
    budget via ``_encode_image_under_budget``.

    Returns ``(data_b64, mime_type)`` unchanged when the input can't be decoded
    as base64 or opened as an image, or when it already fits and needs no resize.
    """
    # Fast path: when no dimension clamp is requested (the non-Anthropic case),
    # an already-under-budget image needs no work — skip the decode entirely.
    # This matters because the send-time policy re-checks the whole history every
    # turn; without it, every image is decoded again on every request. (The
    # Anthropic path passes max_dim, so it must still decode to check dimensions,
    # which can exceed the limit even when the byte budget is met.)
    if max_dim is None and len(data_b64) <= max_b64_bytes:
        return data_b64, mime_type
    try:
        raw = base64.b64decode(data_b64, validate=True)
    except Exception:
        return data_b64, mime_type

    try:
        img = Image.open(BytesIO(raw))
        img.load()
    except Exception:
        return data_b64, mime_type

    changed = False
    if max_dim is not None and max(img.size) > max_dim:
        img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
        changed = True

    if len(data_b64) <= max_b64_bytes and not changed:
        return data_b64, mime_type

    encoded, mime = _encode_image_under_budget(img, max_b64_bytes=max_b64_bytes)
    return base64.b64encode(encoded).decode("ascii"), mime


def normalize_mcp_image_for_anthropic(
    data_b64: str,
    mime_type: str,
    *,
    downscale: bool,
    max_b64_bytes: int | None = None,
) -> tuple[str, str]:
    """Resize/compress MCP tool images for Anthropic tool-result embedding."""
    limit = max_b64_bytes if max_b64_bytes is not None else MAX_IMAGE_BYTES
    max_dim = (
        MAX_ANTHROPIC_MANY_IMAGE_DIMENSION
        if downscale
        else MAX_ANTHROPIC_IMAGE_DIMENSION
    )
    return normalize_mcp_image(
        data_b64, mime_type, max_b64_bytes=limit, max_dim=max_dim
    )


def resolve_non_anthropic_image_cap(model: str) -> int | None:
    """Base64 byte cap for a non-Anthropic model's images, or None (send raw).

    Matches a hosting segment (``NON_ANTHROPIC_IMAGE_B64_CAPS`` key, lowercase)
    against the ``"{provider}/{model}"`` string, so the cap applies regardless of
    transport prefix — e.g. both ``tml/Inkling`` and ``openai/tml/Inkling``
    resolve to the tml cap. The host is always a provider-layer segment, never
    the leaf model name, so we match any segment *except the last* (avoids a
    model whose own name happens to equal a host token) and compare
    case-insensitively (providers are lowercase, but don't rely on casing).
    Returns ``None`` for hosts with no known small cap; those send images raw,
    preserving the pre-existing (uncompressed) behavior.
    """
    provider_segments = {s.lower() for s in model.split("/")[:-1]}
    for host, cap in NON_ANTHROPIC_IMAGE_B64_CAPS.items():
        if host.lower() in provider_segments:
            return cap
    return None


def resolve_remote_image_fetch_cap(model: str) -> int | None:
    """Byte cap the provider applies when IT fetches an image URL, or None.

    Segment-matched exactly like `resolve_non_anthropic_image_cap` — see its
    docstring for why a prefix match is not enough.
    """
    provider_segments = {s.lower() for s in model.split("/")[:-1]}
    for host, cap in REMOTE_IMAGE_FETCH_CAPS.items():
        if host.lower() in provider_segments:
            return cap
    return None


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


def _has_remote_https_image(messages: list[LitellmAnyMessage]) -> bool:
    """True when any message carries a remote https image_url block.

    A read-only scan, so the caller can skip the deep copy. A fetch cap resolves
    for every vertex/gemini model, so without this guard the policy would deep-copy
    the whole history on text-only requests to those providers too.
    """
    for msg in messages:
        content = get_msg_content(msg)
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "image_url":
                continue
            image_url = block.get("image_url")
            url = image_url.get("url") if isinstance(image_url, dict) else image_url
            if isinstance(url, str) and url.startswith(_HTTPS_SCHEMES):
                return True
    return False


def _parse_data_uri(url: str) -> tuple[str, str] | None:
    if not url.startswith("data:") or ";base64," not in url:
        return None
    header, payload = url.split(";base64,", 1)
    mime = header[5:] or "application/octet-stream"
    return mime, payload


def _is_data_uri_image(url: str) -> bool:
    return _parse_data_uri(url) is not None


def _is_remote_https_image(url: str) -> bool:
    """Select every remote https image, deliberately wider than the failing set.

    Narrowing this to litellm's path-extension set would be wrong: Studio's server
    always sets `format` on the block, so litellm takes the `file_uri` branch —
    the one where the cap bites — for ANY https url it builds, extension or not.
    """
    return url.startswith(_HTTPS_SCHEMES)


def _copy_messages_and_collect_image_slots(
    messages: list[LitellmAnyMessage],
    *,
    select: Callable[[str], bool] = _is_data_uri_image,
) -> tuple[list[LitellmAnyMessage], list[_ImageSlot]]:
    """Deep-copy `messages` and return the image_url blocks `select` accepts.

    `select` decides which urls become mutable slots — inline data URIs for the
    two byte-cap policies, remote https urls for the provider-fetch policy.
    """
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
                            if isinstance(url, str) and select(url):
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
                    if isinstance(url, str) and select(url):
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

    copied, slots = _copy_messages_and_collect_image_slots(messages)
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


def apply_non_anthropic_image_policy(
    messages: list[LitellmAnyMessage],
    *,
    model: str,
) -> list[LitellmAnyMessage]:
    """Shrink inline data-URI images for non-Anthropic endpoints with a size cap.

    Complements ``content_blocks_to_messages`` (which caps images at tool-result
    insertion): this runs at the send-time chokepoint and covers images that
    entered the conversation another way — task-embedded ``image_url`` blocks,
    CLI-harness images resolved via ``resolve_to_data_urls`` (capped at 5 MB raw
    ≈ 6.8 MB base64), or persisted history — so a capped endpoint (e.g.
    tml/Inkling) never receives an oversized asset and 400s with
    "Asset is too large" regardless of the image's origin.

    No-op for Anthropic models (handled by ``apply_anthropic_image_policy``) and
    for non-Anthropic providers with no known per-asset cap (they send raw).
    Only rewrites data-URI images that exceed the cap.
    """
    if model.startswith("anthropic/"):
        return messages
    cap = resolve_non_anthropic_image_cap(model)
    if cap is None:
        return messages

    copied, slots = _copy_messages_and_collect_image_slots(messages)
    if not slots:
        return copied

    for slot in slots:
        image_url = slot.block.get("image_url")
        if not isinstance(image_url, dict):
            continue
        url = image_url.get("url")
        if not isinstance(url, str):
            continue
        parsed = _parse_data_uri(url)
        if parsed is None:
            continue
        mime, data_b64 = parsed
        out_b64, out_mime = normalize_mcp_image(data_b64, mime, max_b64_bytes=cap)
        image_url["url"] = f"data:{out_mime};base64,{out_b64}"

    return copied


def _encode_image_prefer_lossless(
    img: Image.Image, *, max_b64_bytes: int
) -> tuple[bytes, str]:
    """Re-encode under budget, trying a LOSSLESS png before any lossy fallback.

    `_encode_image_under_budget` only attempts png for an image carrying alpha.
    That trade is wrong for a prompt image in a coordinate-grounding benchmark, so
    try png for any mode and fall back to the shared ladder only if it cannot fit.
    """
    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    raw = buf.getvalue()
    if _b64_len(raw) <= max_b64_bytes:
        return raw, "image/png"
    return _encode_image_under_budget(img, max_b64_bytes=max_b64_bytes)


async def _remote_image_size(client: httpx.AsyncClient, url: str) -> int | None:
    """Total byte size of `url`, or None when the server does not report it.

    Asks for a single byte and reads the total off `Content-Range`. A presigned S3
    url is signed per-method, so `HEAD` against a GET signature 403s.

    Streamed and never read: a plain `get` would buffer the WHOLE object whenever a
    server ignores `Range` and answers 200, before any size guard could run.

    Returns None for a redirect (the client has redirects disabled so
    `_fetch_streaming` validates each hop), which leaves the url in place.
    """
    if not url.startswith(_HTTPS_SCHEMES):
        return None
    async with client.stream(
        "GET", url, headers={"Range": "bytes=0-0"}, timeout=FETCH_TIMEOUT_SECONDS
    ) as response:
        content_range = response.headers.get("content-range")
        if content_range and "/" in content_range:
            total = content_range.rsplit("/", 1)[1].strip()
            if total.isdigit():
                return int(total)
        # 200 (range ignored) means Content-Length is the whole object.
        if response.status_code == 200:
            length = response.headers.get("content-length")
            if length and length.isdigit():
                return int(length)
    return None


def _per_image_inline_budget(inline_count: int) -> int:
    """Base64 budget for each image inlined into one request.

    The per-image ceiling applies until enough images share the request that the
    aggregate would exceed it, at which point the request budget is split evenly.
    """
    return min(
        REMOTE_IMAGE_REENCODE_B64_BUDGET,
        REMOTE_IMAGE_REQUEST_B64_BUDGET // max(1, inline_count),
    )


async def _probe_size_or_none(client: httpx.AsyncClient, url: str) -> int | None:
    """`_remote_image_size` that reports a failure as None instead of raising.

    The probes run under one `asyncio.gather`, where a raised exception would
    abandon the whole batch. A probe that cannot answer must only mean "leave this
    url alone".
    """
    try:
        return await _remote_image_size(client, url)
    except (httpx.HTTPError, httpx.InvalidURL, ImageFetchError) as exc:
        logger.warning(
            f"remote_image_fetch_policy: size probe failed, leaving the image as "
            f"a url ({type(exc).__name__}: {exc})"
        )
        return None


async def apply_remote_image_fetch_policy(
    messages: list[LitellmAnyMessage],
    *,
    model: str,
    client: httpx.AsyncClient | None = None,
) -> list[LitellmAnyMessage]:
    """Inline a re-encoded copy of any remote image the provider cannot fetch.

    Studio sends task prompt images as presigned https urls and lets the provider
    resolve them. Vertex caps that server-side fetch at 15 MiB and 400s above it,
    which no retry can clear, so an oversized screenshot is ungradeable by Gemini
    while every other provider runs it fine (RLS-10563). Both sibling policies above
    skip these blocks because they only rewrite inline data URIs.

    Only an image measured to be OVER the cap is rewritten, so an in-budget request
    stays byte-identical. Any failure leaves the block alone.
    """
    cap = resolve_remote_image_fetch_cap(model)
    if cap is None:
        return messages

    # Scan before copying: the deep copy is the expensive part and a text-only
    # history has nothing for this policy to rewrite.
    if not _has_remote_https_image(messages):
        return messages

    copied, slots = _copy_messages_and_collect_image_slots(
        messages, select=_is_remote_https_image
    )
    if not slots:
        return copied

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(follow_redirects=False)
    # Enforce it rather than trust the caller: with redirects followed at the client
    # level, neither the probe nor `_fetch_streaming` validates each hop, so an
    # https -> http://internal-host chain would bypass the scheme guard.
    client.follow_redirects = False
    # EVERY outcome, not just a successful inline: a str is the replacement data
    # url, None means "leave this url alone". Successes alone would re-decide the
    # common under-cap url on every occurrence.
    decided: dict[str, str | None] = {}
    try:
        distinct = list(
            dict.fromkeys(
                url
                for slot in slots
                if isinstance(image_url := slot.block.get("image_url"), dict)
                and isinstance(url := image_url.get("url"), str)
                and url.startswith(_HTTPS_SCHEMES)
            )
        )
        # Concurrently: serialized, this cost N_distinct x RTT on the critical path
        # of every request carrying remote images, under cap or not.
        probed = await asyncio.gather(
            *(_probe_size_or_none(client, url) for url in distinct)
        )
        sizes = dict(zip(distinct, probed, strict=True))
        over_cap = {
            url
            for url, size in sizes.items()
            if size is not None and cap < size <= MAX_REMOTE_IMAGE_FETCH_BYTES
        }
        # Divide by OCCURRENCES, not distinct urls: the copy is written into every
        # slot carrying that url, so a url repeated K times costs K times.
        inline_slots = sum(
            1
            for slot in slots
            if isinstance(block_url := slot.block.get("image_url"), dict)
            and isinstance(slot_url := block_url.get("url"), str)
            and slot_url in over_cap
        )
        per_image_budget = _per_image_inline_budget(inline_slots)
        for slot in slots:
            image_url = slot.block.get("image_url")
            if not isinstance(image_url, dict):
                continue
            url = image_url.get("url")
            if not isinstance(url, str) or not url.startswith(_HTTPS_SCHEMES):
                continue
            if url in decided:
                replacement = decided[url]
                if replacement is not None:
                    image_url["url"] = replacement
                continue
            try:
                size = sizes.get(url)
                if size is None or size <= cap:
                    decided[url] = None
                    continue
                if size > MAX_REMOTE_IMAGE_FETCH_BYTES:
                    logger.warning(
                        f"remote_image_fetch_policy: {size} bytes is past the "
                        f"{MAX_REMOTE_IMAGE_FETCH_BYTES}-byte fetch ceiling; "
                        f"leaving the image as a url"
                    )
                    decided[url] = None
                    continue
                # The ceiling, NOT the probed size: `_fetch_streaming` counts
                # DECODED bytes while Content-Range reports the ENCODED length, so
                # an exact-equality cap would trip on any Content-Encoding.
                raw, mime = await _fetch_streaming(
                    client, url, max_bytes=MAX_REMOTE_IMAGE_FETCH_BYTES
                )
                image = Image.open(BytesIO(raw))
                image.load()
                source_size = image.size
                encoded, out_mime = _encode_image_prefer_lossless(
                    image, max_b64_bytes=per_image_budget
                )
                # Header-only open: reports the encoded dimensions without a
                # full decode, so a downscale can never happen silently.
                out_size = Image.open(BytesIO(encoded)).size
            except (
                httpx.HTTPError,
                httpx.InvalidURL,
                ImageFetchError,
                # Subclasses Exception, NOT OSError, so without it a
                # decompression bomb escapes and kills the whole request.
                Image.DecompressionBombError,
                OSError,
                ValueError,
            ) as exc:
                # Leaving the url in place reproduces today's behaviour, which
                # beats dropping the image or erroring the whole run.
                logger.warning(
                    f"remote_image_fetch_policy: leaving oversized image as a url "
                    f"({type(exc).__name__}: {exc})"
                )
                decided[url] = None
                continue
            data_url = (
                f"data:{out_mime};base64,{base64.b64encode(encoded).decode('ascii')}"
            )
            decided[url] = data_url
            image_url["url"] = data_url
            logger.info(
                f"remote_image_fetch_policy: inlined a re-encoded copy "
                f"({size} -> {len(encoded)} bytes, mime={out_mime}, "
                f"{source_size[0]}x{source_size[1]} -> {out_size[0]}x{out_size[1]}"
                f"{' DOWNSCALED' if out_size != source_size else ''}, "
                f"{'lossless' if out_mime == 'image/png' else 'LOSSY'}) because it "
                f"exceeds the {cap}-byte provider fetch cap"
            )
    finally:
        if owns_client:
            await client.aclose()

    return copied


def _to_data_url(raw: bytes, mime: str | None) -> str:
    mime = mime or "application/octet-stream"
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{encoded}"


async def resolve_to_data_urls(
    images: list[dict[str, Any]],
    *,
    client: httpx.AsyncClient | None = None,
    max_image_bytes: int = MAX_IMAGE_BYTES,
) -> list[dict[str, Any]]:
    """Rewrite every entry in `images` so its `url` is a `data:` URL.

    - `data:...` entries pass through unchanged.
    - `https://...` entries are fetched (streamed, capped at `max_image_bytes`)
      and re-encoded.
    - Other schemes (http, s3, file, …) are rejected with ImageFetchError so the
      caller sees a clear contract violation rather than a silent CLI failure
      downstream.

    `max_image_bytes` defaults to MAX_IMAGE_BYTES (the model-context budget cap);
    callers that only need the bytes locally (never sent to an LLM) may raise it.

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
                    raw, mime = await _fetch_streaming(
                        client, url, max_bytes=max_image_bytes
                    )
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
