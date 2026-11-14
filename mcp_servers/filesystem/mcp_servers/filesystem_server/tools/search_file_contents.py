import os
from pathlib import Path
from typing import Annotated

from keyword_search import runtime
from keyword_search.config import load_config
from keyword_search.index import (
    IndexStatus,
    count_indexed_files,
    query_words,
    search_indexes,
)
from mcp_actor import paths as actor_paths
from pydantic import BaseModel, Field
from utils.decorators import make_async_background
from utils.path_utils import PathTraversalError, display_path, resolve_under_root
from utils.structured_output import StructuredText

DEFAULT_MAX_RESULTS = 20
MAX_RESULTS_CAP = 100
NO_INDEX_TEXT = (
    "Keyword search has no index to search yet, so file contents cannot be searched "
    "this way. Use search_files to find files by name, then open them directly."
)


class ContentMatch(BaseModel):
    path: str = Field(
        description="File path, in the same form the other filesystem tools use."
    )
    location: str | None = Field(
        default=None,
        description="Where in the file: page, slide, sheet and rows, paragraphs or lines. Absent for emails.",
    )
    count: int = Field(description="How many times the searched words occur there.")


class SearchFileContentsResult(BaseModel):
    """Structured form of the search_file_contents text output."""

    matches: list[ContentMatch] = Field(
        default_factory=list, description="Best matches first."
    )
    total_matches: int = Field(
        default=0,
        description="Matching sections (pages, slides, ...) across all indexes.",
    )
    files_matched: int = Field(
        default=0, description="Distinct files with at least one match."
    )
    files_searched: int = Field(
        default=0, description="Files whose text is in the searched indexes."
    )
    files_unreadable: int = Field(
        default=0,
        description="Files the indexes could not read (scanned, broken, ...).",
    )
    build_in_progress: bool = Field(
        default=False, description="True while a backup index build is still running."
    )
    error: str | None = Field(
        default=None, description="Present only when the query itself is unusable."
    )


def _relative_prefix(path: str) -> str | None:
    """The index path prefix for ``path``; None when it is outside the sandbox."""
    try:
        resolved = Path(resolve_under_root(path))
    except PathTraversalError:
        return None
    rel = os.path.relpath(resolved, actor_paths.active_filesystem_root())
    return "" if rel == "." else Path(rel).as_posix()


def _caller_sees_target_filesystem() -> bool:
    """Indexes describe the target agent's /filesystem; other actors must not see them."""
    active = Path(actor_paths.active_filesystem_root()).resolve()
    return active == Path(actor_paths.TARGET_AGENT_FILESYSTEM_ROOT).resolve()


def _display(rel: str) -> str:
    root = actor_paths.TARGET_AGENT_FILESYSTEM_ROOT
    return display_path(os.path.join(root, rel), root=root)


def _coverage(indexes: list[IndexStatus]) -> tuple[int, int, list[str]]:
    searched = unreadable = 0
    unreadable_paths: list[str] = []
    for status in indexes:
        searched += sum(
            n for s, n in status.counts.items() if s in ("indexed", "partial")
        )
        unreadable += sum(
            n for s, n in status.counts.items() if s in ("failed", "skipped")
        )
        unreadable_paths.extend(status.unreadable)
    return searched, unreadable, unreadable_paths


@make_async_background
def search_file_contents(
    query: Annotated[
        str,
        Field(
            description="One or more keywords (case-insensitive; punctuation is ignored). A page matches if it contains any of the words; pages where they occur more often are listed first. Example: 'termination fee'."
        ),
    ],
    path: Annotated[
        str,
        Field(
            description="Only return matches in files under this directory. Default: '/' (everything indexed)."
        ),
    ] = "/",
    max_results: Annotated[
        int,
        Field(
            description=f"Most matches to return, best first. Default: {DEFAULT_MAX_RESULTS}; at most {MAX_RESULTS_CAP}."
        ),
    ] = DEFAULT_MAX_RESULTS,
) -> StructuredText[SearchFileContentsResult]:
    """Keyword search inside files (PDF, Word, PowerPoint, Excel, CSV, text, email). Returns the file path, the page/slide/sheet where known, and how many times the keywords occur there. Covers the indexed files, including files added or changed during the run; use search_files to find files by name."""
    words = query_words(query or "")
    if not words:
        text = "[invalid query: give at least one word to search for]"
        return StructuredText(text, SearchFileContentsResult(error=text))
    prefix = _relative_prefix(path or "/")
    if prefix is None:
        text = f"[access denied: {path}]"
        return StructuredText(text, SearchFileContentsResult(error=text))
    limit = min(max(max_results, 1), MAX_RESULTS_CAP)
    if not _caller_sees_target_filesystem():
        return StructuredText(NO_INDEX_TEXT, SearchFileContentsResult())

    config = load_config()
    if count_indexed_files(runtime.working_indexes()) == 0:
        runtime.start_fallback_build_once(config)
    if runtime.build_in_progress():
        runtime.wait_for_build(config.wait_for_build_seconds)
    building = runtime.build_in_progress()
    refreshed = runtime.refresh_built_index(config)

    index_files = runtime.working_indexes()
    outcome = search_indexes(index_files, words, path_prefix=prefix, limit=limit)
    if not outcome.indexes:
        note = NO_INDEX_TEXT
        if building:
            note = "The keyword index is still being built and has nothing searchable yet. Search again shortly."
        return StructuredText(
            note, SearchFileContentsResult(build_in_progress=building)
        )

    searched, unreadable, unreadable_paths = _coverage(outcome.indexes)
    matches = [
        ContentMatch(path=_display(h.path), location=h.location, count=h.count)
        for h in outcome.hits
    ]

    lines: list[str] = []
    if matches:
        shown = (
            f" (showing {len(matches)})"
            if outcome.total_sections > len(matches)
            else ""
        )
        lines.append(
            f"Found {outcome.total_sections} match(es) in {outcome.total_files} file(s) for {query!r}{shown}:\n"
        )
        for number, match in enumerate(matches, start=1):
            where = f" — {match.location}" if match.location else ""
            lines.append(f"{number}. {match.path}{where} ({match.count} occurrence(s))")
    else:
        lines.append(f"No matches for {query!r}.")

    lines.append(f"\nSearched {searched} file(s) in {len(outcome.indexes)} index(es).")
    if outcome.ignored:
        lines.append(
            f"{len(outcome.ignored)} index file(s) could not be searched and were skipped, "
            "so results may be incomplete."
        )
    if unreadable:
        listed = ", ".join(_display(p) for p in unreadable_paths)
        more = (
            f" (+{unreadable - len(unreadable_paths)} more)"
            if unreadable > len(unreadable_paths)
            else ""
        )
        lines.append(
            f"{unreadable} file(s) could not be read for keyword search and are not covered: {listed}{more}. "
            "Open them directly if they may matter."
        )
    if building:
        lines.append(
            f"The keyword index is still being built ({runtime.progress.done} of {runtime.progress.total} files done), "
            "so these results may be incomplete. Search again later for full coverage."
        )
    if refreshed and refreshed.pending:
        lines.append(
            f"{refreshed.pending} file(s) are not indexed yet. "
            "Search again to include them."
        )

    return StructuredText(
        "\n".join(lines),
        SearchFileContentsResult(
            matches=matches,
            total_matches=outcome.total_sections,
            files_matched=outcome.total_files,
            files_searched=searched,
            files_unreadable=unreadable,
            build_in_progress=building,
        ),
    )
