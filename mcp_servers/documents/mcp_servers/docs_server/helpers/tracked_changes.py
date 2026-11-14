"""Tracked changes (OOXML revisions) on the read path.

Word records a revision as markup, not as a text swap: an insertion wraps its runs in
``<w:ins>``, and a deletion keeps the original text inside ``<w:del>`` with each
``<w:t>`` replaced by ``<w:delText>``. A move is the same shape under ``<w:moveTo>`` /
``<w:moveFrom>``.

python-docx's ``paragraph.runs`` yields only the ``<w:r>`` children of ``<w:p>``, so
every run inside that markup is invisible to it. Reading a redlined document that way
returns the sentence with its changed words cut out and nothing to say any were
removed.

Revision runs stay **read-only**. This app has no way to author a revision, so an edit
landing inside someone else's ``<w:ins>`` would silently attribute its text to that
author. ``.r.N`` therefore keeps indexing ``paragraph.runs`` exactly as it did, which is
what ``helpers.mutate.resolve_target`` resolves against, and revision runs carry
``.ins.N`` / ``.del.N`` ids that ``parse_identifier`` rejects.
"""

from dataclasses import dataclass
from typing import Any

from docx.oxml.ns import qn
from docx.text.run import Run

INSERT = "insert"
DELETE = "delete"

_INSERTED_TAGS = (qn("w:ins"), qn("w:moveTo"))
_DELETED_TAGS = (qn("w:del"), qn("w:moveFrom"))
# Children of a run that carry text, in the same translation python-docx applies to
# <w:r>. w:delText is the deleted-text spelling of w:t and is the one it does not read.
_TEXT_TAGS = {
    qn("w:t"): None,
    qn("w:delText"): None,
    qn("w:tab"): "\t",
    qn("w:br"): "\n",
    qn("w:cr"): "\n",
}


@dataclass(frozen=True)
class ParagraphRun:
    """A run a reader should see, with the revision it belongs to (if any)."""

    run: Run
    revision: str | None
    author: str | None
    date: str | None

    @property
    def text(self) -> str:
        """Deleted text lives in ``w:delText``, which ``Run.text`` ignores."""
        if self.revision != DELETE:
            return self.run.text
        parts: list[str] = []
        for child in self.run._element:  # pyright: ignore[reportPrivateUsage]
            if child.tag in _TEXT_TAGS:
                parts.append(_TEXT_TAGS[child.tag] or child.text or "")
        return "".join(parts)


def paragraph_runs(paragraph: Any) -> list[ParagraphRun]:
    """Every run of ``paragraph`` in document order, revisions included.

    Direct ``<w:r>`` children come back unmarked and in the order
    ``paragraph.runs`` gives them, so their positions still line up with it.
    """
    walked: list[ParagraphRun] = []
    for child in paragraph._element:  # pyright: ignore[reportPrivateUsage]
        if child.tag == qn("w:r"):
            walked.append(ParagraphRun(Run(child, paragraph), None, None, None))
            continue
        if child.tag in _INSERTED_TAGS:
            revision = INSERT
        elif child.tag in _DELETED_TAGS:
            revision = DELETE
        else:
            continue
        author = child.get(qn("w:author")) or None
        date = child.get(qn("w:date")) or None
        for run_element in child.findall(qn("w:r")):
            walked.append(
                ParagraphRun(Run(run_element, paragraph), revision, author, date)
            )
    return walked


def count_revisions(paragraphs: Any) -> tuple[int, int]:
    """Insertions and deletions across serialized paragraphs.

    Consecutive runs of one revision count once: Word splits a single edit across
    several runs whenever formatting changes inside it.
    """
    insertions = deletions = 0
    for paragraph in paragraphs:
        previous = None
        for run in paragraph.runs:
            revision = getattr(run, "revision", None)
            if revision and revision != previous:
                if revision == INSERT:
                    insertions += 1
                else:
                    deletions += 1
            previous = revision
    return insertions, deletions
