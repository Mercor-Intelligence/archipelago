"""Utilities for normalizing messages to the OpenAI Responses API input format.

V2 improvements:
- Retry logic on file uploads (acreate_file) with exponential backoff
- Inter-upload delay to avoid bursting the provider's Files API
- Automatic docx/xlsx-to-text conversion for Gemini providers
"""

import asyncio
import base64
import csv
import io
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, cast

from litellm.exceptions import (
    APIConnectionError,
    BadGatewayError,
    InternalServerError,
    RateLimitError,
    ServiceUnavailableError,
    Timeout,
)
from loguru import logger
from pydantic import TypeAdapter

from runner.agents.models import LitellmAnyMessage, LitellmResponsesInputMessage
from runner.utils.decorators import (
    campaign_id_ctx,
    trajectory_batch_id_ctx,
    with_retry,
)
from runner.utils.settings import get_settings


def _trajectory_llm_target_kwargs() -> dict[str, Any]:
    """Resolve the LLM Gateway workload + campaign_id for Files API calls.

    These helpers all run inside a trajectory worker (modal_labs sets the
    ContextVars at the worker entrypoint), so file uploads + deletions
    should ride on the same X-Priority bucket as the actual chat-completion
    call. Without this, file ops land at P3 default while the corresponding
    inference call gets P0/P1 — one trajectory mixes priority classes.
    """
    workload = (
        "trajectory_batch" if trajectory_batch_id_ctx.get() else "trajectory_single"
    )
    return {"workload": workload, "campaign_id": campaign_id_ctx.get()}


_message_adapter: TypeAdapter[LitellmResponsesInputMessage] = TypeAdapter(
    LitellmResponsesInputMessage
)

SUPPORTED_FILE_PROVIDERS = {"openai", "azure", "gemini", "vertex_ai", "bedrock"}

_GEMINI_PROVIDERS = {"gemini", "vertex_ai"}

_INTER_UPLOAD_DELAY_SECONDS = 0.5

_INLINE_TOTAL_THRESHOLD_BYTES = 20 * 1024 * 1024
_GEMINI_INLINE_TOTAL_THRESHOLD_BYTES = 0  # Gemini: always use Files API

# MIME types the Gemini Files API accepts (from litellm GEMINI_1_5_ACCEPTED_FILE_TYPES).
# Files with other types are kept inline rather than uploaded.
_MIME_TO_EXT: dict[str, str] = {
    "application/pdf": "pdf",
    "text/plain": "txt",
    "text/csv": "csv",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "audio/mp3": "mp3",
    "audio/mpeg": "mp3",
    "audio/wav": "wav",
    "audio/flac": "flac",
    "video/mp4": "mp4",
    "video/webm": "webm",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-excel": "xls",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/json": "json",
}

_upload_counter = 0


def _short_upload_filename(mime_type: str) -> str:
    """Generate a short, unique filename for file uploads.

    Gemini's Code Execution derives a filename from the file_uri and rejects
    names longer than 255 chars.  Using a short display name avoids this.
    """
    global _upload_counter
    _upload_counter += 1
    ext = _MIME_TO_EXT.get(mime_type, "bin")
    return f"upload_{_upload_counter}.{ext}"


_GEMINI_SUPPORTED_MIME_TYPES = {
    # Documents
    "application/pdf",
    "text/plain",
    "text/csv",
    # Images
    "image/jpeg",
    "image/png",
    "image/webp",
    # Audio
    "audio/aac",
    "audio/flac",
    "audio/mp3",
    "audio/mpeg",
    "audio/mpga",
    "audio/opus",
    "audio/pcm",
    "audio/wav",
    # Video
    "video/mp4",
    "video/mpeg",
    "video/mov",
    "video/quicktime",
    "video/webm",
    "video/wmv",
    "video/x-flv",
    "video/3gpp",
}


def _extract_provider(model: str) -> str:
    """Extract the provider name from a model string like gemini/gemini-3.1-pro"""
    if "/" in model:
        return model.split("/", 1)[0]
    return model


def _convert_xlsx_to_csv(data: bytes) -> bytes:
    """Best-effort conversion of an Excel workbook to CSV bytes.

    Reads all sheets via openpyxl and writes them as CSV.
    Falls back to the original bytes if conversion fails.
    """
    wb = None
    try:
        import openpyxl

        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)

        all_rows: list[list[str]] = []
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            if len(wb.sheetnames) > 1:
                all_rows.append([f"--- Sheet: {sheet_name} ---"])
            for row in ws.iter_rows(values_only=True):
                all_rows.append([str(cell) if cell is not None else "" for cell in row])

        if not all_rows:
            return data

        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerows(all_rows)
        return buf.getvalue().encode("utf-8")
    except Exception:
        logger.warning("openpyxl conversion failed — falling back to raw bytes")
        return data
    finally:
        if wb is not None:
            wb.close()


def _convert_docx_to_text(data: bytes) -> bytes:
    """Best-effort extraction of text from a .docx file.

    Reads paragraphs and tables via python-docx and joins them with newlines.
    Falls back to the original bytes if extraction fails.
    """
    try:
        import docx

        doc = docx.Document(io.BytesIO(data))
        parts: list[str] = []
        for p in doc.paragraphs:
            if p.text.strip():
                parts.append(p.text)
        for table in doc.tables:
            for row in table.rows:
                cells = [cell.text.strip() for cell in row.cells]
                parts.append("\t".join(cells))

        text = "\n".join(parts)
        return text.encode("utf-8")
    except Exception:
        logger.warning("python-docx extraction failed — falling back to raw bytes")
        return data


_GEMINI_UNSUPPORTED_CONVERSIONS: dict[str, tuple[Callable[[bytes], bytes], str]] = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": (
        _convert_xlsx_to_csv,
        "text/csv",
    ),
    "application/vnd.ms-excel": (_convert_xlsx_to_csv, "text/csv"),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (
        _convert_docx_to_text,
        "text/plain",
    ),
    "application/msword": (_convert_docx_to_text, "text/plain"),
}


def _patch_litellm_file_transform() -> None:
    """Patch litellm to preserve 'format' when converting input_file to file blocks.

    litellm's _transform_input_file_item_to_file_item only copies file_id and
    file_data but drops format/filename, causing Gemini to fail with
    "Unable to determine mime type for file_id". This preserves those fields.
    """
    try:
        from litellm.responses.litellm_completion_transformation.transformation import (
            LiteLLMCompletionResponsesConfig,
        )
    except ImportError:
        return

    original = LiteLLMCompletionResponsesConfig._transform_input_file_item_to_file_item

    @staticmethod  # type: ignore[misc]
    def _patched(item: dict[str, Any]) -> dict[str, Any]:
        result = original(item)
        for key in ("format", "filename"):
            if key in item:
                result["file"][key] = item[key]
        return result

    LiteLLMCompletionResponsesConfig._transform_input_file_item_to_file_item = _patched


_patch_litellm_file_transform()

_GEMINI_FILES_API_HOST = "generativelanguage.googleapis.com"


def _patch_gemini_chat_file_redownload() -> None:
    """Patch litellm's Google AI Studio handler to NOT re-download Gemini Files API URIs.

    litellm's ``GeminiConfig._transform_messages`` converts **all** HTTPS
    ``file_id`` URLs to base64 inline data via ``convert_url_to_base64``.
    For files already uploaded to Gemini's Files API this re-downloads the
    entire file (potentially hundreds of MB) and sends it inline, completely
    defeating the purpose of the Files API and hitting token/quota limits.

    The fix: temporarily replace ``convert_url_to_base64`` during the
    transformation so that Gemini Files API URIs raise an exception.  The
    original code's ``except`` block then leaves ``file_id`` untouched, and
    the downstream Vertex AI handler (``_process_gemini_media``) correctly
    creates a ``file_data.file_uri`` reference for the HTTPS URL.
    """
    try:
        from litellm.llms.gemini.chat import transformation as gemini_chat_mod
        from litellm.llms.gemini.chat.transformation import (
            GoogleAIStudioGeminiConfig as GeminiConfig,
        )
    except ImportError:
        return

    original_transform = GeminiConfig._transform_messages

    def _patched_transform(self: Any, messages: Any, model: Any = None) -> Any:
        original_convert = gemini_chat_mod.convert_url_to_base64

        def _convert_skip_gemini_files(url: str) -> str:
            if _GEMINI_FILES_API_HOST in url:
                raise ValueError("Gemini Files API URI — skip base64 re-download")
            return original_convert(url)

        gemini_chat_mod.convert_url_to_base64 = _convert_skip_gemini_files
        try:
            return original_transform(self, messages, model=model)
        finally:
            gemini_chat_mod.convert_url_to_base64 = original_convert

    GeminiConfig._transform_messages = _patched_transform


_patch_gemini_chat_file_redownload()


def normalize_to_responses_format(
    messages: Sequence[LitellmAnyMessage | dict[str, Any]],
) -> list[LitellmAnyMessage]:
    """
    Normalize Chat Completions-style message content to Responses API input format.

    The RL Studio server converts task inputs into Chat Completions format
    (text, image_url, file), but the Responses API expects its own input types
    (input_text, input_image, input_file). This function bridges the two.

    Converts:
    - {"type": "text", "text": "..."} -> {"type": "input_text", "text": "..."}
    - {"type": "image_url", "image_url": {...}} -> {"type": "input_image", "image_url": "..."}
    - {"type": "file", "file": {...}} -> {"type": "input_file", ...}

    Returns validated LitellmAnyMessage (as EasyInputMessageParam, the Responses API type).
    """
    normalized: list[LitellmAnyMessage] = []

    for msg in messages:
        msg_dict: dict[str, Any] = (
            dict(msg) if isinstance(msg, dict) else msg.model_dump()
        )
        content = msg_dict.get("content")

        if isinstance(content, list):
            remapped: list[Any] = []
            for block in content:
                if isinstance(block, str):
                    remapped.append({"type": "input_text", "text": block})
                    continue

                if not isinstance(block, dict):
                    remapped.append(block)
                    continue

                block_type = block.get("type")

                if block_type == "text":
                    remapped.append(
                        {"type": "input_text", "text": block.get("text", "")}
                    )
                elif block_type == "image_url":
                    image_url = block.get("image_url")
                    detail = "auto"
                    if isinstance(image_url, str):
                        remapped.append(
                            {
                                "type": "input_image",
                                "image_url": image_url,
                                "detail": detail,
                            }
                        )
                    elif isinstance(image_url, dict):
                        url = image_url.get("url")
                        detail = image_url.get("detail", detail)
                        if url:
                            remapped.append(
                                {
                                    "type": "input_image",
                                    "image_url": url,
                                    "detail": detail,
                                }
                            )
                        else:
                            remapped.append(block)
                    else:
                        remapped.append(block)
                elif block_type == "file":
                    file_info = block.get("file") or {}
                    input_file_block: dict[str, Any] = {"type": "input_file"}
                    if file_info.get("file_data"):
                        input_file_block["file_data"] = file_info["file_data"]
                    if file_info.get("file_id"):
                        input_file_block["file_id"] = file_info["file_id"]
                    if file_info.get("filename"):
                        input_file_block["filename"] = file_info["filename"]
                    remapped.append(input_file_block)
                else:
                    remapped.append(block)

            msg_dict["content"] = remapped

        normalized.append(_message_adapter.validate_python(msg_dict))

    return normalized


def denormalize_from_responses_format(
    messages: Sequence[LitellmAnyMessage | dict[str, Any]],
) -> list[dict[str, Any]]:
    """Convert Responses-API input blocks back to Chat Completions content blocks.

    Inverse of :func:`normalize_to_responses_format`. Agents normalize user-message
    content to Responses-API input parts (input_text/input_image/input_file) for the
    API call, but the RL Studio trajectory store and viewer expect Chat Completions
    blocks (text/image_url/file) — persisting the Responses-API shape makes the
    save webhook's serializer reject the content and 500. Run this on the final
    message list before persisting so saved trajectories serialize and render.

    Converts:
    - {"type": "input_text", "text": "..."} -> {"type": "text", "text": "..."}
    - {"type": "input_image", "image_url": "...", "detail": "..."}
        -> {"type": "image_url", "image_url": {"url": "...", "detail": "..."}}
    - {"type": "input_file", file_data?/file_id?/filename?}
        -> {"type": "file", "file": {file_data?/file_id?/filename?}}

    Non-list content and already-Chat-Completions blocks pass through untouched.
    """
    denormalized: list[dict[str, Any]] = []

    for msg in messages:
        msg_dict: dict[str, Any] = (
            dict(msg) if isinstance(msg, dict) else msg.model_dump()
        )
        content = msg_dict.get("content")

        if isinstance(content, list):
            remapped: list[Any] = []
            for block in content:
                if not isinstance(block, dict):
                    remapped.append(block)
                    continue

                block_type = block.get("type")

                if block_type == "input_text":
                    remapped.append({"type": "text", "text": block.get("text", "")})
                elif block_type == "input_image":
                    image_url = block.get("image_url")
                    url = (
                        image_url
                        if isinstance(image_url, str)
                        else (image_url or {}).get("url")
                    )
                    image_url_obj: dict[str, Any] = {"url": url}
                    if block.get("detail"):
                        image_url_obj["detail"] = block["detail"]
                    remapped.append({"type": "image_url", "image_url": image_url_obj})
                elif block_type == "input_file":
                    # `format` is a top-level MIME hint that process_inline_files
                    # stamps onto uploaded/converted blocks; keep it (nested under
                    # `file`, where litellm's input_file->file transform puts it) so
                    # file_id-only blocks don't lose their MIME type on persist.
                    file_obj: dict[str, Any] = {
                        key: block[key]
                        for key in ("file_data", "file_id", "filename", "format")
                        if block.get(key)
                    }
                    remapped.append({"type": "file", "file": file_obj})
                else:
                    remapped.append(block)

            msg_dict["content"] = remapped

        denormalized.append(msg_dict)

    return denormalized


@with_retry(
    max_retries=5,
    base_backoff=3,
    jitter=3,
    retry_on=(
        RateLimitError,
        Timeout,
        APIConnectionError,
        InternalServerError,
        BadGatewayError,
        ServiceUnavailableError,
    ),
)
async def _upload_file_with_retry(
    raw_bytes: bytes,
    mime_type: str,
    provider: str,
    use_proxy: bool = False,
    original_filename: str | None = None,
    **kwargs: Any,
) -> Any:
    """Upload a single file to the provider's Files API with retry + backoff.

    Wraps litellm.acreate_file so that transient errors (rate limits, timeouts,
    server errors) are retried automatically instead of killing the trajectory.

    When ``use_proxy`` is True, the upload is routed through the litellm proxy's
    OpenAI-compatible ``/v1/files`` endpoint.  We tell the litellm *client* to
    use the ``"openai"`` handler (so it POSTs to ``<proxy>/v1/files``) and pass
    the real provider via ``extra_body`` so the *proxy* routes to the correct
    backend (e.g. Gemini Files API).
    """
    from litellm import acreate_file

    # Use original filename if available and within Gemini's 255-char limit,
    # otherwise fall back to a short generated name.
    filename = (
        original_filename
        if original_filename and len(original_filename) <= 255
        else _short_upload_filename(mime_type)
    )

    if use_proxy:
        return await acreate_file(
            file=(filename, io.BytesIO(raw_bytes), mime_type),
            purpose=cast(Any, "user_data"),
            custom_llm_provider=cast(Any, "openai"),
            extra_body={"custom_llm_provider": provider},
            **kwargs,
        )

    return await acreate_file(
        file=(filename, io.BytesIO(raw_bytes), mime_type),
        purpose=cast(Any, "user_data"),
        custom_llm_provider=cast(Any, provider),
        **kwargs,
    )


async def process_inline_files(
    messages: list[LitellmAnyMessage],
    model: str,
) -> list[LitellmAnyMessage]:
    """Process inline file data: convert unsupported types and decide inline vs upload.

    For each ``input_file`` block containing base64 ``file_data``:

    1. Convert unsupported MIME types (docx/xlsx → text/CSV for Gemini).
    2. Compute the **total** decoded size of all inline files across the request.
    3. If the total exceeds the provider threshold, upload **all** files via the
       provider's Files API.  For Gemini the threshold is 0 (always upload)
       because the Files API is free and avoids inflating the request payload
       which counts against TPM quotas.
    """
    provider = _extract_provider(model)
    if provider not in SUPPORTED_FILE_PROVIDERS:
        return messages

    is_gemini = provider in _GEMINI_PROVIDERS
    threshold = (
        _GEMINI_INLINE_TOTAL_THRESHOLD_BYTES
        if is_gemini
        else _INLINE_TOTAL_THRESHOLD_BYTES
    )

    settings = get_settings()
    upload_kwargs: dict[str, Any] = {}
    settings.apply_llm_target(upload_kwargs, **_trajectory_llm_target_kwargs())
    use_proxy = bool(upload_kwargs.get("api_base"))

    # --- Pass 1: decode all inline files, apply format conversions, sum total size ---
    @dataclass
    class _DecodedFile:
        msg_idx: int
        block_idx: int
        raw_bytes: bytes
        mime_type: str
        was_converted: bool
        original_block: dict[str, Any]

    decoded_files: list[_DecodedFile] = []
    total_inline_bytes = 0

    for msg_idx, msg in enumerate(messages):
        msg_dict: dict[str, Any] = (
            dict(msg) if isinstance(msg, dict) else msg.model_dump()
        )
        content = msg_dict.get("content")
        if not isinstance(content, list):
            continue

        for block_idx, block in enumerate(content):
            if not isinstance(block, dict) or block.get("type") != "input_file":
                continue

            file_data = block.get("file_data", "")
            if not isinstance(file_data, str) or not file_data.startswith("data:"):
                continue

            header, _, b64_payload = file_data.partition(",")
            raw_bytes = base64.b64decode(b64_payload)
            mime_type = header.split(";")[0].removeprefix("data:")

            was_converted = False
            if is_gemini and mime_type in _GEMINI_UNSUPPORTED_CONVERSIONS:
                converter, new_mime = _GEMINI_UNSUPPORTED_CONVERSIONS[mime_type]
                original_bytes = raw_bytes
                raw_bytes = converter(raw_bytes)

                if raw_bytes is original_bytes:
                    logger.warning(
                        f"Conversion to {new_mime} failed — "
                        f"keeping original MIME type {mime_type}"
                    )
                else:
                    mime_type = new_mime
                    was_converted = True
                    logger.info(f"Converted unsupported Gemini MIME type to {new_mime}")

            total_inline_bytes += len(raw_bytes)
            decoded_files.append(
                _DecodedFile(
                    msg_idx=msg_idx,
                    block_idx=block_idx,
                    raw_bytes=raw_bytes,
                    mime_type=mime_type,
                    was_converted=was_converted,
                    original_block=block,
                )
            )

    if not decoded_files:
        return messages

    use_files_api = total_inline_bytes > threshold
    logger.info(
        f"Total inline file bytes: {total_inline_bytes} across {len(decoded_files)} file(s), "
        f"threshold: {threshold}, upload_all: {use_files_api}"
    )

    # --- Pass 2: build replacement blocks (upload or re-encode) ---
    # Map (msg_idx, block_idx) → replacement block
    replacements: dict[tuple[int, int], dict[str, Any]] = {}
    upload_count = 0

    for df in decoded_files:
        if use_files_api:
            if upload_count > 0:
                await asyncio.sleep(_INTER_UPLOAD_DELAY_SECONDS)

            logger.info(
                f"Uploading file ({len(df.raw_bytes)} bytes, {df.mime_type}) "
                f"via Files API to {provider}"
            )
            uploaded = await _upload_file_with_retry(
                raw_bytes=df.raw_bytes,
                mime_type=df.mime_type,
                provider=provider,
                use_proxy=use_proxy,
                original_filename=df.original_block.get("filename"),
                **upload_kwargs,
            )
            upload_count += 1

            replacement: dict[str, Any] = {
                "type": "input_file",
                "file_id": uploaded.id,
            }
            if df.mime_type:
                replacement["format"] = df.mime_type
            replacements[(df.msg_idx, df.block_idx)] = replacement
        elif df.was_converted:
            # File was format-converted but stays inline — re-encode
            new_b64 = base64.b64encode(df.raw_bytes).decode("ascii")
            replacement = {
                "type": "input_file",
                "file_data": f"data:{df.mime_type};base64,{new_b64}",
                "format": df.mime_type,
            }
            if df.original_block.get("filename"):
                replacement["filename"] = df.original_block["filename"]
            replacements[(df.msg_idx, df.block_idx)] = replacement

    if not replacements:
        return messages

    # --- Pass 3: rebuild messages with replacements ---
    result: list[LitellmAnyMessage] = []
    for msg_idx, msg in enumerate(messages):
        msg_dict = dict(msg) if isinstance(msg, dict) else msg.model_dump()
        content = msg_dict.get("content")

        if not isinstance(content, list):
            result.append(msg)
            continue

        has_replacement = any(
            (msg_idx, bi) in replacements for bi in range(len(content))
        )
        if not has_replacement:
            result.append(msg)
            continue

        new_content = [
            replacements.get((msg_idx, bi), block) for bi, block in enumerate(content)
        ]
        msg_dict["content"] = new_content
        result.append(cast(LitellmAnyMessage, cast(object, msg_dict)))

    if upload_count:
        logger.info(f"Files API uploads: {upload_count}")
    return result


async def delete_gemini_files(
    file_ids: list[str],
    model: str,
) -> None:
    """Best-effort deletion of files previously uploaded to the Gemini Files API.

    Accepts the file IDs (URIs) returned by ``process_chat_completion_files``
    and deletes them so they don't count against the 20 GB storage quota.
    Errors are logged but never raised.
    """
    if not file_ids:
        return

    provider = _extract_provider(model)
    if provider not in _GEMINI_PROVIDERS:
        return

    settings = get_settings()
    _delete_kwargs: dict[str, Any] = {}
    settings.apply_llm_target(_delete_kwargs, **_trajectory_llm_target_kwargs())
    use_proxy = bool(_delete_kwargs.get("api_base"))

    for file_id in file_ids:
        try:
            from litellm import adelete_file

            if use_proxy:
                await adelete_file(
                    file_id=file_id,
                    custom_llm_provider=cast(Any, "openai"),
                    extra_body={"custom_llm_provider": provider},
                    **_delete_kwargs,
                )
            else:
                await adelete_file(
                    file_id=file_id,
                    custom_llm_provider=cast(Any, provider),
                )
            logger.info(f"Deleted uploaded file {file_id}")
        except Exception:
            logger.warning(f"Failed to delete uploaded file {file_id}")


async def process_chat_completion_files(
    messages: list[LitellmAnyMessage],
    model: str,
) -> tuple[list[LitellmAnyMessage], list[str]]:
    """Upload inline files in Chat Completions format via the Files API.

    Scans messages for ``{"type": "file", "file": {"file_data": "data:...;base64,..."}}``
    blocks, uploads them, and replaces with ``file_id`` references.

    For Gemini all files are uploaded (threshold=0).  For other providers files
    are uploaded only when the total inline size exceeds 20 MB.

    Unlike ``process_inline_files`` (which operates on Responses API
    ``input_file`` blocks), this works on Chat Completions ``file`` blocks
    and preserves ``format`` directly — no lossy conversion step.

    Returns:
        A tuple of (processed_messages, uploaded_file_ids).  The caller should
        pass ``uploaded_file_ids`` to ``delete_gemini_files`` after the model
        call to free storage quota.
    """
    provider = _extract_provider(model)
    if provider not in _GEMINI_PROVIDERS:
        return messages, []

    settings = get_settings()
    upload_kwargs: dict[str, Any] = {}
    settings.apply_llm_target(upload_kwargs, **_trajectory_llm_target_kwargs())
    use_proxy = bool(upload_kwargs.get("api_base"))

    # --- Pass 1: find all inline file blocks, decode, sum total size ---
    @dataclass
    class _DecodedFile:
        msg_idx: int
        block_idx: int
        raw_bytes: bytes
        mime_type: str
        original_block: dict[str, Any]

    decoded_files: list[_DecodedFile] = []
    total_inline_bytes = 0

    for msg_idx, msg in enumerate(messages):
        msg_dict: dict[str, Any] = (
            dict(msg) if isinstance(msg, dict) else msg.model_dump()
        )
        content = msg_dict.get("content")
        if not isinstance(content, list):
            continue

        for block_idx, block in enumerate(content):
            if not isinstance(block, dict) or block.get("type") != "file":
                continue

            file_info = block.get("file") or {}
            file_data = file_info.get("file_data", "")
            if not isinstance(file_data, str) or not file_data.startswith("data:"):
                continue

            header, _, b64_payload = file_data.partition(",")
            raw_bytes = base64.b64decode(b64_payload)
            mime_type = file_info.get("format") or header.split(";")[0].removeprefix(
                "data:"
            )

            # Convert Gemini-unsupported MIME types (docx/xlsx) before upload
            if mime_type in _GEMINI_UNSUPPORTED_CONVERSIONS:
                converter, new_mime = _GEMINI_UNSUPPORTED_CONVERSIONS[mime_type]
                original_bytes = raw_bytes
                raw_bytes = converter(raw_bytes)
                if raw_bytes is not original_bytes:
                    mime_type = new_mime
                    logger.info(f"Converted unsupported Gemini MIME type to {new_mime}")

            # Skip files whose MIME type isn't supported by Gemini Files API
            if mime_type not in _GEMINI_SUPPORTED_MIME_TYPES:
                logger.info(
                    f"Keeping file inline — unsupported Gemini MIME type {mime_type}"
                )
                continue

            total_inline_bytes += len(raw_bytes)
            decoded_files.append(
                _DecodedFile(
                    msg_idx=msg_idx,
                    block_idx=block_idx,
                    raw_bytes=raw_bytes,
                    mime_type=mime_type,
                    original_block=block,
                )
            )

    if not decoded_files:
        return messages, []

    logger.info(
        f"Chat Completions files: {total_inline_bytes} bytes across "
        f"{len(decoded_files)} file(s), uploading all via Files API"
    )

    # --- Pass 2: upload and build replacements ---
    replacements: dict[tuple[int, int], dict[str, Any]] = {}
    uploaded_file_ids: list[str] = []
    upload_count = 0

    try:
        for df in decoded_files:
            if upload_count > 0:
                await asyncio.sleep(_INTER_UPLOAD_DELAY_SECONDS)

            logger.info(
                f"Uploading file ({len(df.raw_bytes)} bytes, {df.mime_type}) "
                f"via Files API to {provider}"
            )
            uploaded = await _upload_file_with_retry(
                raw_bytes=df.raw_bytes,
                mime_type=df.mime_type,
                provider=provider,
                use_proxy=use_proxy,
                original_filename=df.original_block.get("file", {}).get("filename"),
                **upload_kwargs,
            )
            upload_count += 1
            uploaded_file_ids.append(uploaded.id)

            replacements[(df.msg_idx, df.block_idx)] = {
                "type": "file",
                "file": {
                    "file_id": uploaded.id,
                    "format": df.mime_type,
                },
            }
    except Exception:
        # Clean up any files that were uploaded before the failure
        await delete_gemini_files(uploaded_file_ids, model)
        raise

    # --- Pass 3: rebuild messages ---
    result: list[LitellmAnyMessage] = []
    for msg_idx, msg in enumerate(messages):
        msg_dict = dict(msg) if isinstance(msg, dict) else msg.model_dump()
        content = msg_dict.get("content")

        if not isinstance(content, list):
            result.append(msg)
            continue

        has_replacement = any(
            (msg_idx, bi) in replacements for bi in range(len(content))
        )
        if not has_replacement:
            result.append(msg)
            continue

        new_content = [
            replacements.get((msg_idx, bi), block) for bi, block in enumerate(content)
        ]
        msg_dict["content"] = new_content
        result.append(cast(LitellmAnyMessage, cast(object, msg_dict)))

    if upload_count:
        logger.info(f"Files API uploads: {upload_count}")
    return result, uploaded_file_ids
