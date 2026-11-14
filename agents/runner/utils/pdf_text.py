"""Send inline PDFs as extracted text to providers that reject PDF file blocks.

Fireworks' chat completions 400s on any ``file`` block, and xAI behind Vercel 400s
on inline non-image files, so a task that attaches a PDF fails on its first call
with a non-retriable error. Those requests can never succeed as sent, so swapping
the PDF for its text changes nothing that works today.
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import hashlib
import threading
from collections import OrderedDict
from typing import Any, cast

from loguru import logger

from runner.agents.models import LitellmAnyMessage

# PyMuPDF is not thread-safe: every runner parse must go through this one worker.
PDF_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="runner-pdf"
)
PDF_TEXT_TIMEOUT = 30.0
# Provider segments of the ``{provider}/{model}`` string, matched like the image caps.
INLINE_PDF_UNSUPPORTED_HOSTS = frozenset({"fireworks_ai", "spacexai"})
_PDF_DATA_PREFIX = "data:application/pdf;base64,"
# History is resent every turn; keyed by digest so the base64 itself is not retained.
_TEXT_CACHE: OrderedDict[str, str | None] = OrderedDict()
_TEXT_CACHE_SIZE = 32
_CACHE_LOCK = threading.Lock()


class _Miss:
    pass


_MISS = _Miss()


def rejects_inline_pdf(model: str) -> bool:
    return bool(
        {s.lower() for s in model.split("/")[:-1]} & INLINE_PDF_UNSUPPORTED_HOSTS
    )


def _pdf_text(file_data: str) -> str | None:
    try:
        import pymupdf

        raw = base64.b64decode(file_data[len(_PDF_DATA_PREFIX) :], validate=True)
        with pymupdf.open(stream=raw, filetype="pdf") as doc:
            pages = [str(page.get_text("text") or "").strip() for page in doc]
    except Exception as exc:
        logger.warning(f"Could not extract text from inline PDF: {exc}")
        return None
    return "\n\n".join(p for p in pages if p) or None


def _digest(file_data: str) -> str:
    return hashlib.sha256(file_data.encode()).hexdigest()


def _cache_get(key: str) -> str | None | _Miss:
    with _CACHE_LOCK:
        if key not in _TEXT_CACHE:
            return _MISS
        _TEXT_CACHE.move_to_end(key)
        return _TEXT_CACHE[key]


def _cached_pdf_text(key: str, file_data: str) -> str | None:
    """Runs on PDF_EXECUTOR; rechecks so a queued duplicate miss reuses the first parse."""
    hit = _cache_get(key)
    if not isinstance(hit, _Miss):
        return hit
    text = _pdf_text(file_data)
    with _CACHE_LOCK:
        _TEXT_CACHE[key] = text
        if len(_TEXT_CACHE) > _TEXT_CACHE_SIZE:
            _TEXT_CACHE.popitem(last=False)
    return text


def _pdf_data(block: Any) -> str | None:
    if not isinstance(block, dict) or block.get("type") != "file":
        return None
    file_data = (block.get("file") or {}).get("file_data")
    if isinstance(file_data, str) and file_data.startswith(_PDF_DATA_PREFIX):
        return file_data
    return None


async def _as_text_block(block: Any) -> Any:
    file_data = _pdf_data(block)
    if file_data is None:
        return block
    # Off the loop (hashlib releases the GIL) and off the PDF worker.
    key = await asyncio.to_thread(_digest, file_data)
    # Hits skip the worker so they never queue behind another parse.
    text = _cache_get(key)
    if isinstance(text, _Miss):
        try:
            text = await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(
                    PDF_EXECUTOR, _cached_pdf_text, key, file_data
                ),
                timeout=PDF_TEXT_TIMEOUT,
            )
        except TimeoutError:
            # Sending the PDF is a non-retriable 400; use text another caller stored meanwhile.
            text = _cache_get(key)
            if isinstance(text, _Miss):
                logger.warning(
                    f"Inline PDF text extraction exceeded {PDF_TEXT_TIMEOUT:.0f}s"
                )
                return block
    if text is None:
        return block  # unreadable/scanned: send as before
    name = block["file"].get("filename") or "attachment.pdf"
    return {"type": "text", "text": f"[Attached PDF: {name}]\n\n{text}"}


async def inline_pdfs_as_text(
    messages: list[LitellmAnyMessage],
) -> list[LitellmAnyMessage]:
    """Replace inline PDF file blocks with their extracted text. Input is not mutated."""
    out: list[LitellmAnyMessage] = []
    for msg in messages:
        content: Any = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(msg, dict) and isinstance(content, list):
            blocks = cast(list[Any], content)
            if any(_pdf_data(b) for b in blocks):
                msg_copy: dict[str, Any] = dict(msg)
                msg_copy["content"] = [await _as_text_block(b) for b in blocks]
                out.append(msg_copy)  # pyright: ignore[reportArgumentType]
                continue
        out.append(msg)
    return out
