"""Per-slide speaker notes, attached to the slide that owns them.

Notes live in `ppt/notesSlides/`, which neither the local extractor's shape
walk nor Reducto's render of the deck reads. Both tiers attach the same block
to the same slide's sub-artifact, so slide-level attribution survives the
flattening that drops the whole-deck artifact.
"""

from __future__ import annotations

import hashlib
import io
from collections.abc import Sequence
from typing import Any

from loguru import logger
from pptx import Presentation

SLIDE_NOTES_HEADING = "=== Speaker notes ==="
NO_SLIDE_NOTES = "(no speaker notes on this slide)"
UNREADABLE_SLIDE_NOTES = "(this slide's notes could not be read)"

# Per slide, not per deck: one real 30-slide deck carries 192k characters of
# notes, so a deck-level cap would silently drop its later slides entirely.
# ~3x the heaviest slide observed there, and the truncation says so in the text.
MAX_SLIDE_NOTES_CHARS = 20_000


def slide_notes_text(slide: Any) -> str:
    """The notes body for one slide, or "" when it has none."""
    # Reading `notes_slide` creates the part, so every deck would gain notes.
    if not getattr(slide, "has_notes_slide", False):
        return ""
    # `notes_text_frame` is the notes placeholder alone, excluding the
    # slide-image, slide-number and date placeholders beside it.
    frame = slide.notes_slide.notes_text_frame
    if frame is None:
        return ""
    return str(frame.text).strip()


def _block(text: str) -> str:
    body = text or NO_SLIDE_NOTES
    if len(body) > MAX_SLIDE_NOTES_CHARS:
        # Snapshot-diff change detection compares this extracted text and
        # nothing else, so without the digest an edit past the cap is invisible.
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
        body = (
            f"{body[:MAX_SLIDE_NOTES_CHARS]}\n"
            f"... [speaker notes truncated at {MAX_SLIDE_NOTES_CHARS:,} of "
            f"{len(body):,} characters; sha256 {digest}]"
        )
    return f"{SLIDE_NOTES_HEADING}\n{body}"


def slide_notes_blocks(slides: Sequence[Any]) -> dict[int, str]:
    """The notes block to append to each slide's text, by 0-based slide index.

    Emitted for every slide, silent ones included. A block that appeared only
    once a deck had notes somewhere would make every silent slide read as
    modified the moment any one slide gained a note, and would leave a judge
    unable to tell a slide with no notes from one whose notes it was never
    shown. Never raises: a slide whose notes will not parse is reported as
    that slide's notes, not as the deck's.
    """
    blocks: dict[int, str] = {}
    for index, slide in enumerate(slides):
        try:
            text = slide_notes_text(slide)
        except Exception as exc:  # noqa: BLE001 — degrade per slide, not per deck
            logger.warning(f"slide {index + 1} speaker notes could not be read: {exc}")
            text = UNREADABLE_SLIDE_NOTES
        blocks[index] = _block(text)
    return blocks


def slide_notes_blocks_from_bytes(file_bytes: bytes) -> dict[int, str]:
    """`slide_notes_blocks` for a deck held as bytes. Never raises."""
    try:
        presentation = Presentation(io.BytesIO(file_bytes))
        slides = list(presentation.slides)
    except Exception as exc:  # noqa: BLE001 — a deck we cannot open has no notes
        logger.warning(f"speaker notes could not be read from the deck: {exc}")
        return {}
    return slide_notes_blocks(slides)


def with_slide_notes(slide_text: str, notes_block: str) -> str:
    """`slide_text` with this slide's notes block appended."""
    if not notes_block:
        return slide_text
    return f"{slide_text}\n\n{notes_block}" if slide_text else notes_block
