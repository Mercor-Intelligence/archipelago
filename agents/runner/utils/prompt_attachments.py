"""Task-prompt attachments, in the shape a CLI-wrapping MCP app can accept.

Every CLI harness has the same gap: the CLI takes a prompt string, so anything
an expert attached to the task — a PDF, a spreadsheet, an image — has to be
written to disk by the app and named in the prompt. The apps
(mercor-rls-opencode-agent, mercor-rls-claude-agent) both take the same
three-field attachment (``filename``/``format``/``file_data``) and both
materialize it the same way, so the harness-side extraction belongs in one
place rather than once per agent.

That sharing is the point, not a tidiness preference: SkillsBench grades a
WithSkill and a NoSkill arm of the same task, and those arms can run on
different harnesses. An attachment one harness forwards and the other silently
drops would make the pair incomparable for reasons that have nothing to do
with the skill under test.

Two block shapes arrive from Studio's message conversion
(``models/msg_representations.py``):

* ``{"type": "file", "file": {...}}`` — every non-image attachment. Carries its
  bytes inline; the server has already resolved S3 to base64.
* ``{"type": "image_url", "image_url": {...}}`` — images. May carry a data URI
  or a remote https URL, so resolving it is async.

Both become the same attachment dict, because both apps write both kinds of
file to disk and let the agent's own read tool pick a renderer.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from runner.utils.image_fetch import ImageFetchError, resolve_to_data_urls

# Last-resort media type for a block that carries neither an explicit `format`
# nor a media type in its data URI.
DEFAULT_FILE_FORMAT = "application/octet-stream"

# Images are written to disk and read back by the agent rather than inlined
# into a model request, so the model-context budget that governs
# `resolve_to_data_urls` by default does not apply. The ceiling that does apply
# is the apps' own per-file attachment cap (25 MB in both), and this sits under
# it so a rejection surfaces here — where the error names the image — rather
# than inside the MCP call.
MAX_IMAGE_ATTACHMENT_BYTES = 20 * 1024 * 1024


def _data_uri_decoded_size(file_data: str) -> int:
    """Decoded byte length of a base64 data URI, from its payload length."""
    header, _, payload = file_data.partition(",")
    if ";base64" not in header:
        return len(payload.encode("utf-8"))
    return max(len(payload) * 3 // 4 - payload.count("="), 0)


def _format_from_data_uri(file_data: str) -> str:
    """The media type declared by a data URI, or "" when it is not one."""
    if not file_data.startswith("data:"):
        return ""
    return file_data.split(";", 1)[0].removeprefix("data:")


def _iter_content_blocks(messages: list[Any]) -> Iterator[dict[str, Any]]:
    """Every dict content block across all messages, in message-then-block order."""
    for msg in messages:
        content = (
            msg.get("content")
            if isinstance(msg, dict)
            else getattr(msg, "content", None)
        )
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict):
                yield part


def _attachment(
    *, file_data: str, file_format: str | None, filename: Any
) -> dict[str, str]:
    """One attachment dict in the shape both MCP apps accept.

    Normalizes the two fields the apps require, because upstream only
    guarantees them on the server's own conversion path:

    * ``file_data`` is emitted as a full ``data:<mime>;base64,<payload>`` URI. A
      bare base64 payload is a shape the server accepts inbound, so a block
      without the ``data:`` prefix is wrapped, not dropped — the apps reject
      anything that is not a base64 data URI.
    * ``format`` falls back to the data URI's own media type, then to
      ``application/octet-stream``.

    ``filename`` is optional on both sides and is passed through only when the
    source block carries a non-blank one. No name is invented here: each app
    derives ``attachment-<n>.<ext>`` for a nameless file and sanitizes supplied
    and derived names through one code path, so a second scheme here would only
    shadow the better one. A blank name is omitted rather than sent as ``""``
    or ``"   "``, so that derivation path actually triggers instead of
    receiving a blank name it has to treat as supplied. The name itself is
    passed through unchanged — spaces and parentheses are the expert's real
    filename, and the app is what reduces it to a safe basename.
    """
    resolved_format = file_format
    if not isinstance(resolved_format, str) or not resolved_format:
        resolved_format = _format_from_data_uri(file_data) or DEFAULT_FILE_FORMAT

    data = file_data
    if not data.startswith("data:"):
        data = f"data:{resolved_format};base64,{data}"

    entry: dict[str, str] = {"format": resolved_format, "file_data": data}
    if isinstance(filename, str) and filename.strip():
        entry["filename"] = filename
    return entry


def extract_file_attachments(messages: list[Any]) -> list[dict[str, str]]:
    """Every ``type: "file"`` content block, as attachment dicts.

    A document an expert attaches via "Upload to context" reaches the agent as
    a litellm file block, which the text-only `extract_task` drops on the floor
    because it is not text — so the attachment silently never reached the model
    (OBI-85).

    Synchronous: file blocks always carry their bytes inline (the server has
    already resolved S3 to base64), so there is no remote URL to fetch. A block
    with no inline ``file_data`` — a provider-side ``file_id`` reference, say —
    has no bytes to forward and is skipped. Malformed blocks are skipped rather
    than raised: a bad attachment must not take down the run.
    """
    files: list[dict[str, str]] = []
    for part in _iter_content_blocks(messages):
        if part.get("type") != "file":
            continue
        file_info = part.get("file")
        if not isinstance(file_info, dict):
            continue
        file_data = file_info.get("file_data")
        if not isinstance(file_data, str) or not file_data:
            continue
        files.append(
            _attachment(
                file_data=file_data,
                file_format=file_info.get("format"),
                filename=file_info.get("filename"),
            )
        )
    return files


def extract_image_blocks(messages: list[Any]) -> list[dict[str, Any]]:
    """Every ``image_url`` content block, unresolved, in order.

    Handles both shapes seen in Studio multimodal tasks::

        {type: "image_url", image_url: {url: "data:...", detail?: "high"}}
        {type: "image_url", image_url: "https://..."}

    URLs are NOT resolved here — see `extract_image_attachments` for the
    resolved form, and the claude_code agent's `extract_images` for the shape
    its own `images` field takes.
    """
    images: list[dict[str, Any]] = []
    for part in _iter_content_blocks(messages):
        if part.get("type") != "image_url":
            continue
        image_url = part.get("image_url")
        if isinstance(image_url, dict):
            url = image_url.get("url")
            detail = image_url.get("detail")
        else:
            url = image_url
            detail = None
        if not isinstance(url, str) or not url:
            continue
        entry: dict[str, Any] = {"url": url}
        if detail is not None:
            entry["detail"] = detail
        images.append(entry)
    return images


async def extract_image_attachments(messages: list[Any]) -> list[dict[str, str]]:
    """Every ``image_url`` block as an attachment dict, remote URLs resolved.

    For harnesses whose app has no separate image channel: the image becomes a
    file on disk like any other attachment, and the agent's read tool renders
    it (opencode's Read handles image/* and PDF natively). Async because an
    `image_url` block may carry a remote https URL, unlike a file block.

    Raises ImageFetchError when a URL cannot be resolved or an image exceeds
    the attachment cap — an image an expert put in the task is content the
    prompt is about, so a silent drop would grade the agent on a task it was
    never shown.
    """
    blocks = extract_image_blocks(messages)
    if not blocks:
        return []
    resolved = await resolve_to_data_urls(
        blocks, max_image_bytes=MAX_IMAGE_ATTACHMENT_BYTES
    )
    for index, entry in enumerate(resolved):
        size = _data_uri_decoded_size(entry["url"])
        if size > MAX_IMAGE_ATTACHMENT_BYTES:
            raise ImageFetchError(
                f"Image index {index} is {size} bytes, exceeding the "
                f"{MAX_IMAGE_ATTACHMENT_BYTES}-byte attachment cap"
            )
    # `detail` is dropped: it is a vision-API hint, and these images are being
    # handed over as files on disk where it has no meaning.
    return [
        _attachment(file_data=entry["url"], file_format=None, filename=None)
        for entry in resolved
    ]
