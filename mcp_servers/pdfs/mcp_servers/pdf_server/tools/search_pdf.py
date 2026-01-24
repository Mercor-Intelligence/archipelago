import os
import re

import pypdf
from mcp_schema import GeminiBaseModel as BaseModel
from pydantic import ConfigDict, Field
from utils.decorators import make_async_background

PDF_ROOT = os.getenv("APP_PDF_ROOT") or os.getenv("APP_FS_ROOT", "/filesystem")


def _resolve_under_root(path: str) -> tuple[str, str | None]:
    """Map path to the PDF root with security checks.

    Returns:
        Tuple of (resolved_path, error_message). If error_message is not None,
        the path is invalid and should not be used.
    """
    path = path.lstrip("/")
    full_path = os.path.join(PDF_ROOT, path)

    # Normalize the path
    normalized_path = os.path.normpath(full_path)

    # Security check: ensure the normalized path is still under PDF_ROOT
    normalized_root = os.path.normpath(PDF_ROOT)
    if (
        not normalized_path.startswith(normalized_root + os.sep)
        and normalized_path != normalized_root
    ):
        return "", "Path traversal detected: path cannot escape PDF root"

    return normalized_path, None


class SearchMatch(BaseModel):
    """A single search match result."""

    model_config = ConfigDict(extra="forbid")

    page: int = Field(..., description="Page number (1-indexed)")
    line: int = Field(..., description="Line number within page (1-indexed)")
    char_start: int = Field(..., description="Character start position in line")
    char_end: int = Field(..., description="Character end position in line")
    context: str = Field(..., description="Text context around the match")

    def __str__(self) -> str:
        return f"[Page {self.page}, Line {self.line}, Chars {self.char_start}-{self.char_end}]: {self.context}"


class SearchResult(BaseModel):
    """Search results for PDF text search."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., description="Search query")
    total_matches: int = Field(..., description="Total number of matches found")
    matches: list[SearchMatch] = Field(
        default_factory=list, description="List of search matches"
    )
    error: str | None = Field(None, description="Error message if search failed")

    def __str__(self) -> str:
        if self.error:
            return f"Search failed: {self.error}"

        if self.total_matches == 0:
            return f'No matches found for "{self.query}"'

        lines = [f'Found {self.total_matches} match(es) for "{self.query}":', ""]

        for idx, match in enumerate(self.matches, 1):
            lines.append(f"{idx}. {match}")

        return "\n".join(lines)


def _search_in_text(
    text: str,
    query: str,
    page_num: int,
    case_sensitive: bool,
    whole_documents: bool,
    context_chars: int,
) -> list[SearchMatch]:
    """Search for query in text and return matches with positions.

    Args:
        text: Text to search in
        query: Search query
        page_num: Page number for results
        case_sensitive: Whether to match case
        whole_documents: Whether to match whole words only
        context_chars: Number of characters to show around match

    Returns:
        List of SearchMatch objects
    """
    matches = []
    lines = text.split("\n")

    for line_idx, line in enumerate(lines, 1):
        search_text = line if case_sensitive else line.lower()
        search_query = query if case_sensitive else query.lower()

        if whole_documents:
            # Use documents boundaries for whole documents matching
            pattern = (
                r"\b" + re.escape(search_query) + r"\b"
                if case_sensitive
                else r"(?i)\b" + re.escape(search_query) + r"\b"
            )
            documents_matches = re.finditer(pattern, line)

            for match in documents_matches:
                char_start = match.start()
                char_end = match.end()

                # Extract context
                context_start = max(0, char_start - context_chars)
                context_end = min(len(line), char_end + context_chars)
                context = line[context_start:context_end].strip()

                # Add ellipsis if context is truncated
                if context_start > 0:
                    context = "..." + context
                if context_end < len(line):
                    context = context + "..."

                matches.append(
                    SearchMatch(
                        page=page_num,
                        line=line_idx,
                        char_start=char_start,
                        char_end=char_end,
                        context=context,
                    )
                )
        else:
            # Simple substring search
            pos = 0
            while True:
                pos = search_text.find(search_query, pos)
                if pos == -1:
                    break

                char_start = pos
                char_end = pos + len(search_query)

                # Extract context
                context_start = max(0, char_start - context_chars)
                context_end = min(len(line), char_end + context_chars)
                context = line[context_start:context_end].strip()

                # Add ellipsis if context is truncated
                if context_start > 0:
                    context = "..." + context
                if context_end < len(line):
                    context = context + "..."

                matches.append(
                    SearchMatch(
                        page=page_num,
                        line=line_idx,
                        char_start=char_start,
                        char_end=char_end,
                        context=context,
                    )
                )

                pos = char_end  # Move past this match

    return matches


@make_async_background
def search_pdf(
    file_path: str,
    query: str,
    case_sensitive: bool = False,
    whole_documents: bool = False,
    max_results: int = 100,
    context_chars: int = 50,
) -> str:
    """Search for text in a PDF document (like Ctrl+F)."""

    # Validate inputs
    if not isinstance(file_path, str) or not file_path:
        return str(
            SearchResult(
                query=query or "", total_matches=0, error="File path is required"
            )
        )

    if not file_path.startswith("/"):
        return str(
            SearchResult(
                query=query or "", total_matches=0, error="File path must start with /"
            )
        )

    if not file_path.lower().endswith(".pdf"):
        return str(
            SearchResult(
                query=query or "", total_matches=0, error="File path must end with .pdf"
            )
        )

    if not isinstance(query, str) or not query:
        return str(
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
        return str(SearchResult(query=query, total_matches=0, error=path_error))

    try:
        # Check file exists
        if not os.path.exists(target_path):
            return str(
                SearchResult(
                    query=query, total_matches=0, error=f"File not found: {file_path}"
                )
            )

        if not os.path.isfile(target_path):
            return str(
                SearchResult(
                    query=query, total_matches=0, error=f"Not a file: {file_path}"
                )
            )

        # Read PDF and search
        all_matches = []

        with open(target_path, "rb") as f:
            pdf_reader = pypdf.PdfReader(f)
            total_pages = len(pdf_reader.pages)

            for page_num in range(1, total_pages + 1):
                try:
                    page = pdf_reader.pages[page_num - 1]  # 0-indexed
                    page_text = page.extract_text(extraction_mode="layout")

                    if page_text:
                        page_matches = _search_in_text(
                            page_text,
                            query,
                            page_num,
                            case_sensitive,
                            whole_documents,
                            context_chars,
                        )
                        all_matches.extend(page_matches)

                        # Stop if we've reached max_results
                        if len(all_matches) >= max_results:
                            all_matches = all_matches[:max_results]
                            break

                except Exception:
                    # Skip pages that fail to extract
                    continue

        result = SearchResult(
            query=query, total_matches=len(all_matches), matches=all_matches, error=None
        )

        return str(result)

    except Exception as exc:
        return str(
            SearchResult(
                query=query, total_matches=0, error=f"Search failed: {repr(exc)}"
            )
        )
