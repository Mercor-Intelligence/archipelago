"""Pagination utilities for document processing.

A page holds as many paragraphs as fit under a content budget, rather than a fixed
number of them. A document of 50 one-line paragraphs and a document of 50 page-long
paragraphs are not the same size, and a fixed count treats them as if they were — which
is how a read ends up too large to return.

Sizing pages by content means a page cannot grow past what a response can carry, so an
oversized read is impossible by construction rather than something to detect and refuse.
"""

# Content budget for one page. This is a page-size knob, not a failure threshold: set it
# lower and documents have more pages, set it higher and they have fewer. Getting it
# wrong changes how a document is divided, it does not make a read fail.
PAGE_TARGET_CHARS = 40_000

# Hard ceiling on a single response. Content-sized pages do not normally approach it; it
# is a backstop for the one case packing cannot solve — a single element too large to
# split, which is reported rather than emitted.
MAX_INLINE_CHARS = 50_000

# A paragraph's serialized form costs more than its text: an element id, run boundaries,
# style and formatting fields. Pages are packed from this estimate instead of by
# serializing, because serializing every paragraph would compress every image in the
# document on every call. The estimate is deliberately generous, so a packed page lands
# under the budget rather than over it.
_PARAGRAPH_OVERHEAD = 90
_RUN_OVERHEAD = 70


def paragraph_cost(paragraph) -> int:
    """Estimated size of a paragraph in the serialized response."""
    runs = getattr(paragraph, "runs", None) or ()
    return len(paragraph.text) + _PARAGRAPH_OVERHEAD + _RUN_OVERHEAD * len(runs)


def table_cost(table) -> int:
    """Estimated size of a table in the serialized response."""
    total = _PARAGRAPH_OVERHEAD
    for row in table.rows:
        for cell in row.cells:
            total += _PARAGRAPH_OVERHEAD
            for paragraph in cell.paragraphs:
                total += paragraph_cost(paragraph)
    return total


def paginate(paragraphs, reserved: int = 0) -> list[tuple[int, int]]:
    """Pack paragraphs into content-sized pages.

    Returns (start, end) paragraph index pairs, always at least one page even for an
    empty document. A paragraph too large to share a page gets one to itself, so packing
    always makes progress.

    `reserved` is content carried on the first page — the document's tables — and is
    charged against that page's budget so the first page does not overrun.
    """
    # Never let reserved content starve the first page entirely; it still takes at least
    # a quarter of the budget's worth of paragraphs.
    budget = max(PAGE_TARGET_CHARS - reserved, PAGE_TARGET_CHARS // 4)

    pages: list[tuple[int, int]] = []
    start = 0
    used = 0

    for i, paragraph in enumerate(paragraphs):
        cost = paragraph_cost(paragraph)
        if i > start and used + cost > budget:
            pages.append((start, i))
            start = i
            used = 0
            budget = PAGE_TARGET_CHARS
        used += cost

    pages.append((start, len(paragraphs)))
    return pages


def calculate_total_pages(paragraphs, reserved: int = 0) -> int:
    """How many content-sized pages a document's paragraphs pack into (at least 1)."""
    return len(paginate(paragraphs, reserved))
