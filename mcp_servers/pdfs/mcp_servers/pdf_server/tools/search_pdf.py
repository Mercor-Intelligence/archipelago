import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Annotated, cast

import fitz  # PyMuPDF - much faster than pypdf for text extraction
from loguru import logger
from pydantic import ConfigDict, Field
from utils.decorators import make_async_background
from utils.ocr import ocr_available, ocr_page_image, ocr_unavailable_reason
from utils.path_utils import PathTraversalError, resolve_under_root
from utils.pdf_protection import (
    PdfPasswordIncorrectError,
    PdfPasswordRequiredError,
    password_error_message,
    unlock_pdf_bytes,
)
from utils.pdf_symbols import recover_symbol_characters, unnamed_count
from utils.schema import OutputBaseModel
from utils.structured_output import StructuredText

# Parallelization settings
MAX_WORKERS = 4  # Number of worker threads for parallel page processing
PARALLEL_THRESHOLD = 5  # Only parallelize if more than this many pages

# A document to search: its physical path, or -- for a password-protected PDF --
# the decrypted bytes, which exist only for the duration of one call.
PdfSource = str | bytes


def _open_pdf(source: PdfSource) -> fitz.Document:
    if isinstance(source, bytes):
        return fitz.open(stream=source, filetype="pdf")
    return fitz.open(source)


def _page_list(pages: list[int]) -> str:
    shown = ", ".join(str(n) for n in pages[:20])
    more = "..." if len(pages) > 20 else ""
    return f"{shown}{more}"


def _unread_pages_error(failed: list[int], total_pages: int) -> str:
    """Name the pages whose text could not be extracted, so were not searched."""
    if len(failed) >= total_pages:
        return (
            f"text could not be extracted from any of the document's {total_pages} "
            f"page(s), so nothing was searched (pages {_page_list(failed)})"
        )
    return (
        f"{len(failed)} of {total_pages} page(s) could not be read and were not "
        f"searched (pages {_page_list(failed)})"
    )


def _unnamed_glyphs_warning(pages: list[int]) -> str:
    """Name the pages drawing symbols no font on them could put a character to.

    Those symbols stay in the context as the control characters extraction
    produced, so the caller has to be told they stand for something the page
    shows rather than read them as the document's own text. A warning rather
    than an error: every page was still searched.
    """
    return (
        f"{len(pages)} page(s) draw symbols whose character could not be recovered; "
        f"they appear in the context as control characters, not as the symbol "
        f"(pages {_page_list(pages)})"
    )


def _resolve_under_root(path: str) -> tuple[str, str | None]:
    """Resolve a virtual path against the active actor's root.

    Returns:
        Tuple of (resolved_path, error_message). If error_message is not None,
        the path is invalid and should not be used.
    """
    try:
        return resolve_under_root(path), None
    except PathTraversalError:
        return "", "Path traversal detected: path cannot escape PDF root"


class SearchMatch(OutputBaseModel):
    """A single search match result."""

    model_config = ConfigDict(extra="forbid")

    page: int = Field(
        ...,
        description="1-indexed page number where this match was found (e.g., 1 for first page).",
    )
    line: int = Field(
        ...,
        description="1-indexed line number within the page where match was found. Lines are determined by newline characters in extracted text, which may differ from visual lines in the PDF.",
    )
    char_start: int = Field(
        ...,
        description="0-indexed character position where the match starts within the line. First character of line is position 0.",
    )
    char_end: int = Field(
        ...,
        description="0-indexed character position where the match ends (exclusive, like Python slice). Match text is line[char_start:char_end].",
    )
    context: str = Field(
        ...,
        description="Text surrounding the match, with the match included. Truncated to context_chars on each side. Prefixed with '...' if truncated at start, suffixed with '...' if truncated at end.",
    )

    def __str__(self) -> str:
        return f"[Page {self.page}, Line {self.line}, Chars {self.char_start}-{self.char_end}]: {self.context}"


class SearchResult(OutputBaseModel):
    """Search results for PDF text search."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        ...,
        description="The original search query string that was used for this search.",
    )
    total_matches: int = Field(
        ...,
        description="Total number of matches found and returned. May equal max_results if search was truncated. Does not represent total matches in document if limit was reached.",
    )
    matches: list[SearchMatch] = Field(
        default_factory=list,
        description="List of SearchMatch objects, each containing location and context for one match. Ordered by page number, then line number, then character position. Empty list if no matches found.",
    )
    error: str | None = Field(
        None,
        description="Error message string if search failed (e.g., file not found, invalid path, password-protected PDF with a missing or wrong password). Also set when there were zero matches and image-only pages could not be OCRed, and -- alongside the matches that were found -- when some pages could not be read and were not searched. None if every page was searched (even with zero matches).",
    )
    warning: str | None = Field(
        default=None,
        description="Set when the search did cover the whole document but something in it could not be read faithfully: a page draws a symbol whose character could not be recovered, so it appears in a context as a control character instead of as the symbol. Unlike `error` this never means the search failed or that any page went unsearched, so a zero-match result carrying only a warning really is zero matches. None when nothing was degraded.",
    )

    def __str__(self) -> str:
        if self.error and not self.matches:
            return f"Search failed: {self.error}"

        if self.total_matches == 0:
            lines = [f'No matches found for "{self.query}"']
        else:
            lines = [f'Found {self.total_matches} match(es) for "{self.query}":', ""]
            for idx, match in enumerate(self.matches, 1):
                lines.append(f"{idx}. {match}")

        # Partial search: the matches are real, but the caller must not read
        # their count as covering the pages that were never searched. A warning
        # reaches a zero-match result too -- that search did cover the document,
        # so saying so is the point of keeping it out of ``error``.
        for note in (self.error, self.warning):
            if note:
                lines.extend(["", f"Warning: {note}"])

        return "\n".join(lines)


def _search_in_text(
    text: str,
    query: str,
    page_num: int,
    case_sensitive: bool,
    whole_word: bool,
    context_chars: int,
) -> list[SearchMatch]:
    """Search for query in text and return matches with positions.

    Args:
        text: Text to search in
        query: Search query
        page_num: Page number for results
        case_sensitive: Whether to match case
        whole_word: Whether to match whole words only
        context_chars: Number of characters to show around match

    Returns:
        List of SearchMatch objects
    """
    matches = []
    lines = text.split("\n")

    # Precompute absolute character offset for each line in the full text
    line_offsets: list[int] = []
    offset = 0
    for line in lines:
        line_offsets.append(offset)
        offset += len(line) + 1  # +1 for the \n

    for line_idx, line in enumerate(lines):
        search_text = line if case_sensitive else line.lower()
        search_query = query if case_sensitive else query.lower()

        if whole_word:
            pattern = (
                r"\b" + re.escape(search_query) + r"\b"
                if case_sensitive
                else r"(?i)\b" + re.escape(search_query) + r"\b"
            )
            word_matches = re.finditer(pattern, line)

            for match in word_matches:
                char_start = match.start()
                char_end = match.end()

                abs_start = line_offsets[line_idx] + char_start
                abs_end = line_offsets[line_idx] + char_end
                ctx_start = max(0, abs_start - context_chars)
                ctx_end = min(len(text), abs_end + context_chars)
                context = text[ctx_start:ctx_end].replace("\n", " ").strip()
                if ctx_start > 0:
                    context = "..." + context
                if ctx_end < len(text):
                    context = context + "..."

                matches.append(
                    SearchMatch(
                        page=page_num,
                        line=line_idx + 1,
                        char_start=char_start,
                        char_end=char_end,
                        context=context,
                    )
                )
        else:
            pos = 0
            while True:
                pos = search_text.find(search_query, pos)
                if pos == -1:
                    break

                char_start = pos
                char_end = pos + len(search_query)

                abs_start = line_offsets[line_idx] + char_start
                abs_end = line_offsets[line_idx] + char_end
                ctx_start = max(0, abs_start - context_chars)
                ctx_end = min(len(text), abs_end + context_chars)
                context = text[ctx_start:ctx_end].replace("\n", " ").strip()
                if ctx_start > 0:
                    context = "..." + context
                if ctx_end < len(text):
                    context = context + "..."

                matches.append(
                    SearchMatch(
                        page=page_num,
                        line=line_idx + 1,
                        char_start=char_start,
                        char_end=char_end,
                        context=context,
                    )
                )

                pos = char_end

    return matches


def _search_page_text(
    page_text: str,
    page_num: int,
    query: str,
    case_sensitive: bool,
    whole_word: bool,
    context_chars: int,
) -> list[SearchMatch]:
    """Search a single page's text - designed to be called in parallel."""
    if page_text:
        return _search_in_text(
            page_text,
            query,
            page_num,
            case_sensitive,
            whole_word,
            context_chars,
        )
    return []


def _extract_page_text_with_ocr_fallback(
    page: fitz.Page, page_num: int
) -> tuple[str, int]:
    """Extract text from a page; use OCR fallback when page is image-only.

    Returns the text with the characters of its symbol glyphs recovered, and
    how many of those glyphs no font on the page could name.

    Recovering a symbol is the last thing done to a page that has already been
    read, so it is never allowed to cost the page its text: a page whose
    recovery raises keeps what extraction gave it -- the control characters the
    search has always had -- and is reported as one whose symbols could not be
    recovered. The alternative is the exception reaching ``_read_page_text``,
    which would file the page among those that could not be read at all and
    drop every match on it.
    """
    page_text = cast(str, page.get_text("text")) or ""
    if page_text.strip():
        try:
            return recover_symbol_characters(page, page_text)
        except Exception as exc:
            logger.debug(f"search_pdf: page {page_num} symbol recovery failed: {exc!r}")
            return page_text, unnamed_count(page_text)
    # Try OCR for scanned/image-only pages
    if ocr_available():
        try:
            mat = fitz.Matrix(2, 2)  # 2x zoom for better OCR
            pix = page.get_pixmap(matrix=mat)
            img_bytes = pix.tobytes("png")
            ocr_text = ocr_page_image(img_bytes, format="PNG")
            if ocr_text:
                return ocr_text, 0
        except Exception:
            pass
    return "", 0


def _read_page_text(
    doc: fitz.Document, page_num: int, failed: list[int], unnamed: list[int]
) -> str | None:
    """One page's text, or None (with the page recorded in ``failed``) if it raised.

    A page that cannot be read is not a page without the query on it. Recording
    it is what lets the result say which pages were never searched instead of
    folding them into the match count. A page recorded in ``unnamed`` was read,
    but kept control characters where it draws a symbol.
    """
    try:
        page_text, unnamed_glyphs = _extract_page_text_with_ocr_fallback(
            doc[page_num - 1], page_num
        )
    except Exception as exc:
        logger.debug(f"search_pdf: page {page_num} text extraction failed: {exc!r}")
        failed.append(page_num)
        return None
    if unnamed_glyphs:
        unnamed.append(page_num)
    return page_text


def _extract_all_page_texts(
    source: PdfSource,
) -> tuple[list[tuple[int, str]], list[int], list[int]]:
    """Extract text from all pages in a single PDF open.

    Returns ``(page_texts, failed_pages, unnamed_glyph_pages)``: ``(page_num,
    text)`` for every page that could be read, the page numbers whose
    extraction raised, and those left holding an unrecovered symbol.
    """
    page_texts: list[tuple[int, str]] = []
    failed: list[int] = []
    unnamed: list[int] = []
    doc = _open_pdf(source)
    try:
        for page_num in range(1, len(doc) + 1):
            page_text = _read_page_text(doc, page_num, failed, unnamed)
            if page_text is not None:
                page_texts.append((page_num, page_text))
    finally:
        doc.close()
    return page_texts, failed, unnamed


def _search_sequential(
    source: PdfSource,
    total_pages: int,
    query: str,
    case_sensitive: bool,
    whole_word: bool,
    context_chars: int,
    max_results: int,
) -> tuple[list[SearchMatch], list[int], list[int]]:
    """Sequential page processing for small PDFs.

    Returns (matches, failed_pages, unnamed_glyph_pages).
    """
    all_matches: list[SearchMatch] = []
    failed: list[int] = []
    unnamed: list[int] = []

    doc = _open_pdf(source)
    try:
        for page_num in range(1, total_pages + 1):
            page_text = _read_page_text(doc, page_num, failed, unnamed)
            if page_text:
                page_matches = _search_in_text(
                    page_text,
                    query,
                    page_num,
                    case_sensitive,
                    whole_word,
                    context_chars,
                )
                all_matches.extend(page_matches)

                if len(all_matches) >= max_results:
                    return all_matches[:max_results], failed, unnamed
    finally:
        doc.close()

    return all_matches, failed, unnamed


def _search_parallel(
    source: PdfSource,
    query: str,
    case_sensitive: bool,
    whole_word: bool,
    context_chars: int,
    max_results: int,
) -> tuple[list[SearchMatch], list[int], list[int]]:
    """Parallel page processing for large PDFs.

    Returns (matches, failed_pages, unnamed_glyph_pages). Extracts all page
    texts first (single PDF open), then parallelizes the search.
    """
    # Step 1: Extract all page texts with a single PDF open (I/O bound)
    page_texts, failed, unnamed = _extract_all_page_texts(source)

    # Step 2: Search in parallel across extracted texts (CPU bound)
    all_matches: list[SearchMatch] = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(
                _search_page_text,
                page_text,
                page_num,
                query,
                case_sensitive,
                whole_word,
                context_chars,
            ): page_num
            for page_num, page_text in page_texts
        }

        for future in as_completed(futures):
            page_matches = future.result()
            all_matches.extend(page_matches)

    # Sort since parallel results are unordered
    all_matches.sort(key=lambda m: (m.page, m.line, m.char_start))
    return all_matches[:max_results], failed, unnamed


def _ocr_gap_error(source: PdfSource, skip: frozenset[int] = frozenset()) -> str | None:
    """Explain a zero-match search that skipped image-only pages because OCR cannot run.

    Pages in ``skip`` already failed extraction and are reported on their own.
    """
    reason = ocr_unavailable_reason()
    if reason is None:
        return None
    image_only: list[int] = []
    try:
        doc = _open_pdf(source)
    except Exception:
        return None
    try:
        for n in range(1, len(doc) + 1):
            if n in skip:
                continue
            if not str(doc[n - 1].get_text("text") or "").strip():
                image_only.append(n)
    finally:
        doc.close()
    if not image_only:
        return None
    return (
        f"{reason}; {len(image_only)} page(s) have no text layer and were not "
        f"searched (pages {_page_list(image_only)})"
    )


def _result(result: SearchResult) -> StructuredText[SearchResult]:
    """The tool's text, str(result), carrying result as its structured form."""
    return StructuredText(str(result), result)


@make_async_background
def search_pdf(
    file_path: Annotated[
        str,
        Field(
            description="Absolute path to the PDF file. Must start with '/' and end with '.pdf'."
        ),
    ],
    query: Annotated[
        str,
        Field(
            description="The text string to search for within the PDF. Must be non-empty."
        ),
    ],
    case_sensitive: Annotated[
        bool,
        Field(
            description="If True, match the exact letter casing of the query. Defaults to False (case-insensitive)."
        ),
    ] = False,
    whole_word: Annotated[
        bool,
        Field(
            description="If True, only match the query as a complete word (bounded by word boundaries). Defaults to False."
        ),
    ] = False,
    max_results: Annotated[
        int,
        Field(
            description="Maximum number of matches to return. Defaults to 100. Must be a positive integer."
        ),
    ] = 100,
    context_chars: Annotated[
        int,
        Field(
            description="Number of characters of context before and after each match. Defaults to 50."
        ),
    ] = 50,
    password: Annotated[
        str | None,
        Field(
            description="Password for a password-protected (encrypted) PDF. Omit it for "
            "ordinary PDFs and for PDFs that open with an empty password. Without it a "
            "protected PDF returns an error (never a silent zero-match result); a wrong "
            "password returns a distinct 'password ... is incorrect' error."
        ),
    ] = None,
) -> StructuredText[SearchResult]:
    """Search for text occurrences across all pages of a PDF document.

    Extracts text from each page and performs substring or whole-word
    matching against the query. Uses parallel processing for PDFs with
    more than a few pages. Returns match count, positions (page, line,
    character offsets), and surrounding context for each hit. Pages whose
    text could not be extracted are named in a warning. For a
    password-protected PDF, pass its password in ``password``.
    """

    # Validate inputs
    if not isinstance(file_path, str) or not file_path:
        return _result(
            SearchResult(
                query=query or "", total_matches=0, error="File path is required"
            )
        )

    if not file_path.startswith("/"):
        return _result(
            SearchResult(
                query=query or "", total_matches=0, error="File path must start with /"
            )
        )

    if not file_path.lower().endswith(".pdf"):
        return _result(
            SearchResult(
                query=query or "", total_matches=0, error="File path must end with .pdf"
            )
        )

    if not isinstance(query, str) or not query:
        return _result(
            SearchResult(
                query=query or "", total_matches=0, error="Search query is required"
            )
        )

    if not isinstance(max_results, int) or max_results < 1:
        max_results = 100

    if not isinstance(context_chars, int) or context_chars < 0:
        context_chars = 50

    # Resolve path with security check
    target_path, path_error = _resolve_under_root(file_path)
    if path_error:
        return _result(SearchResult(query=query, total_matches=0, error=path_error))

    try:
        # Check file exists
        if not os.path.exists(target_path):
            return _result(
                SearchResult(
                    query=query, total_matches=0, error=f"File not found: {file_path}"
                )
            )

        if not os.path.isfile(target_path):
            return _result(
                SearchResult(
                    query=query, total_matches=0, error=f"Not a file: {file_path}"
                )
            )

        # Get total pages first
        source: PdfSource = target_path
        doc = fitz.open(target_path)
        needs_password = bool(doc.needs_pass)
        total_pages = len(doc)
        doc.close()

        # Every page of a locked document raises on extraction. Searching it
        # without decrypting would report zero matches on a document never read
        # -- an answer indistinguishable from the term genuinely being absent.
        # pypdf decrypts; PyMuPDF then searches the plain bytes (its own
        # authenticate() leaves real task documents unreadable).
        if needs_password:
            with open(target_path, "rb") as f:
                encrypted = f.read()
            try:
                plain, _ = unlock_pdf_bytes(encrypted, password)
            except (PdfPasswordRequiredError, PdfPasswordIncorrectError) as exc:
                return _result(
                    SearchResult(
                        query=query,
                        total_matches=0,
                        error=password_error_message(file_path, exc),
                    )
                )
            doc = fitz.open(stream=plain, filetype="pdf")
            still_locked = bool(doc.needs_pass)
            total_pages = len(doc)
            doc.close()
            if still_locked:
                return _result(
                    SearchResult(
                        query=query,
                        total_matches=0,
                        error=password_error_message(
                            file_path, PdfPasswordRequiredError()
                        ),
                    )
                )
            source = plain

        # Use sequential for small PDFs, parallel for large ones
        if total_pages <= PARALLEL_THRESHOLD:
            all_matches, failed_pages, unnamed_pages = _search_sequential(
                source,
                total_pages,
                query,
                case_sensitive,
                whole_word,
                context_chars,
                max_results,
            )
        else:
            all_matches, failed_pages, unnamed_pages = _search_parallel(
                source,
                query,
                case_sensitive,
                whole_word,
                context_chars,
                max_results,
            )

        error = None
        if failed_pages:
            error = _unread_pages_error(failed_pages, total_pages)
        if not all_matches and len(failed_pages) < total_pages:
            gap = _ocr_gap_error(source, frozenset(failed_pages))
            if gap:
                error = f"{error}; {gap}" if error else gap

        result = SearchResult(
            query=query,
            total_matches=len(all_matches),
            matches=all_matches,
            error=error,
            warning=_unnamed_glyphs_warning(unnamed_pages) if unnamed_pages else None,
        )

        return _result(result)

    except Exception as exc:
        # The library's own message (e.g. "Failed to open file '...'") is kept;
        # its repr (exception class, quoting) is not, and SearchResult already
        # prefixes "Search failed:" -- the old f"Search failed: {exc!r}" printed
        # that prefix twice.
        logger.warning(f"search_pdf: unexpected failure on {file_path}: {exc!r}")
        detail = str(exc).strip() or "the PDF could not be opened or searched"
        return _result(SearchResult(query=query, total_matches=0, error=detail))
