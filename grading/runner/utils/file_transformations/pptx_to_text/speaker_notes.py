"""Speaker-notes text from a .pptx package.

Read here rather than inside a file-extraction backend: notes live in
`ppt/notesSlides/`, which no backend reads, and which backend serves a given
read depends on the credentials the environment happens to have.
"""

from __future__ import annotations

import asyncio
import io

from loguru import logger
from pptx import Presentation

from ..style_metadata_cache import cached_style_text, is_ooxml_package

NOTES_HEADING = "=== Speaker notes ==="
NO_NOTES = "(this presentation contains no speaker notes)"
NOT_OOXML = "(speaker notes could not be read: this file is not an OOXML .pptx package)"
UNREADABLE_SLIDE = "(this slide's notes could not be read)"

# The deck is written by the agent under grading, so the notes are unbounded
# input; capped, and the cap disclosed in the text, per this package's README.
MAX_NOTES_CHARS = 200_000

# Separate cache namespace so notes and style facts can never share an entry.
_CACHE_KIND = "pptx_speaker_notes"


def _slide_notes(slide: object) -> str:
    """The notes body for one slide, or "" when it has none."""
    # Reading `notes_slide` creates the part, so every deck would gain notes.
    if not getattr(slide, "has_notes_slide", False):
        return ""
    # `notes_text_frame` is the notes placeholder alone, excluding the
    # slide-image, slide-number and date placeholders beside it.
    frame = slide.notes_slide.notes_text_frame  # pyright: ignore[reportAttributeAccessIssue]
    if frame is None:
        return ""
    return str(frame.text).strip()


def speaker_notes_section(file_bytes: bytes) -> str:
    """The notes block for a presentation, in slide order. Never raises.

    Always returns one of three outcomes — notes, none, or unreadable — so a
    judge can tell an empty notes region from one it could not see. One slide
    that will not parse is reported as that slide's notes, not as the deck's.
    """
    try:
        presentation = Presentation(io.BytesIO(file_bytes))
        slides = list(presentation.slides)
    except Exception as exc:  # noqa: BLE001 — the judge should see the failure
        logger.warning(f"speaker notes could not be read from the deck: {exc}")
        return f"{NOTES_HEADING}\n(speaker notes could not be read: {exc})"

    written: list[tuple[int, str]] = []
    for number, slide in enumerate(slides, start=1):
        try:
            text = _slide_notes(slide)
        except Exception as exc:  # noqa: BLE001 — degrade per slide, not per deck
            logger.warning(f"slide {number} speaker notes could not be read: {exc}")
            text = UNREADABLE_SLIDE
        if text:
            written.append((number, text))

    if not written:
        return f"{NOTES_HEADING}\n{NO_NOTES}"

    body = "\n\n".join(
        f"--- Slide {number} notes ---\n{text}" for number, text in written
    )
    if len(body) > MAX_NOTES_CHARS:
        body = (
            f"{body[:MAX_NOTES_CHARS]}\n"
            f"... [speaker notes truncated at {MAX_NOTES_CHARS:,} of "
            f"{len(body):,} characters]"
        )
    return f"{NOTES_HEADING}\n{body}"


async def _extract_speaker_notes(file_bytes: bytes, file_name: str) -> str:
    # Parsing is CPU-bound; keep it off the shared grading event loop.
    return await asyncio.to_thread(speaker_notes_section, file_bytes)


async def with_speaker_notes(
    text: str | None, file_bytes: bytes, file_name: str
) -> str | None:
    """`text` with the deck's speaker-notes block appended."""
    if not is_ooxml_package(file_bytes):
        # Legacy .ppt is OLE2 and .odp is a different zip layout; python-pptx
        # opens neither, so there is nothing to parse or cache.
        section = f"{NOTES_HEADING}\n{NOT_OOXML}"
    else:
        # Same single-flight coalescing the style extractors use: a rubric can
        # run ~70 verifiers over one deck concurrently.
        section = await cached_style_text(
            file_bytes, file_name, _CACHE_KIND, _extract_speaker_notes
        )
    return f"{text}\n\n{section}" if text else section
