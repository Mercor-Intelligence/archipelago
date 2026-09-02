"""Plain-text chunking for indexing / retrieval.

Splits extracted document text into bounded, slightly-overlapping chunks so
each piece embeds and indexes cleanly while preserving local context. The
splitter prefers paragraph (``\\n\\n``) then sentence (``". "``) boundaries
near the target size, falling back to a hard cut. App-agnostic and dependency
free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Defaults tuned for sentence-transformer style embedders (a few hundred words
# per chunk) while keeping FTS rows small enough to rank well.
DEFAULT_CHUNK_CHARS = 4000
DEFAULT_CHUNK_OVERLAP = 400
DEFAULT_MAX_CHUNKS = 200


@dataclass
class ChunkRecord:
    """One chunk of a document's text.

    Attributes:
        chunk_id: Stable id ``"<doc_id>:<index>"``.
        doc_id: Owning document id (content-addressed ``file_id``).
        index: Zero-based position of this chunk within the document.
        text: Chunk text (stripped).
        char_count: Length of ``text`` in characters.
        meta: Arbitrary passthrough metadata.
    """

    chunk_id: str
    doc_id: str
    index: int
    text: str
    char_count: int
    meta: dict[str, Any] = field(default_factory=dict)


def chunk_text(
    text: str,
    *,
    doc_id: str,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
    max_chunks: int = DEFAULT_MAX_CHUNKS,
) -> list[ChunkRecord]:
    """Split ``text`` into overlapping :class:`ChunkRecord` pieces.

    Returns an empty list for blank input. Short text yields a single chunk.
    Splits prefer a paragraph break, then a sentence break, occurring past the
    half-way point of the window; otherwise the window is cut at ``chunk_chars``.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= chunk_chars:
        return [ChunkRecord(f"{doc_id}:0", doc_id, 0, text, len(text))]

    chunks: list[ChunkRecord] = []
    start = 0
    idx = 0
    while start < len(text) and idx < max_chunks:
        end = min(start + chunk_chars, len(text))
        if end < len(text):
            split_at = text.rfind("\n\n", start, end)
            if split_at <= start + chunk_chars // 2:
                split_at = text.rfind(". ", start, end)
            if split_at > start + chunk_chars // 2:
                end = split_at + 1
        piece = text[start:end].strip()
        if piece:
            chunks.append(ChunkRecord(f"{doc_id}:{idx}", doc_id, idx, piece, len(piece)))
            idx += 1
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks
