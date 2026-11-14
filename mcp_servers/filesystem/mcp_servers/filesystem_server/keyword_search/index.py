"""The keyword index file format, plus building and searching it.

An index is one SQLite file holding an FTS5 table. The same builder runs in the app's
``populate`` task and in ``scripts/build_keyword_index.py``, so an index built ahead
of time and one built in a run are the same format. Format version 3:

- ``meta(key, value)``: ``format`` = ``keyword-index``, ``version`` = ``3``,
  ``text_chars`` (characters indexed so far, for the total text limit),
  ``state`` = ``building`` | ``complete`` | ``failed``; once complete also
  ``built_by`` (``populate`` | ``search_fallback`` | ``script``), ``build_seconds``
  and ``built_at``.
- ``files(path, size, mtime_ns, status, detail)``: one row per file the build chose.
  ``status`` is ``pending`` | ``indexed`` | ``partial`` | ``skipped`` | ``failed``.
  ``size`` and ``mtime_ns`` let ``refresh_index`` spot files changed since.
- ``sections`` (contentless FTS5) holds the words of each page, slide, sheet
  block or line block; ``sections_meta(id, path, location)`` says where row ``id``
  came from. No document text is stored, only the word index. A contentless
  table cannot delete rows, so a refresh drops a file's ``sections_meta`` rows and
  its old words stay behind unreachable; ``AUTOINCREMENT`` keeps their ids unused. ``path`` is
  relative to the filesystem root, '/'-separated, with no leading slash.
"""

import os
import re
import sqlite3
import time
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from keyword_search.extractors import (
    COST_ORDER,
    MAX_CHARS_PER_FILE,
    ExtractionError,
    NoTextError,
    TextLimitReached,
    TimeLimitReached,
    extract_sections,
    file_type,
)

FORMAT_NAME = "keyword-index"
FORMAT_VERSION = "3"
INDEX_SUFFIX = ".sqlite"

_COMMIT_EVERY_FILES = 25
_COMMIT_EVERY_SECONDS = 2.0
_BUSY_TIMEOUT_MS = 5000
_WORD_RE = re.compile(r"[^\W_]+")
_MAX_QUERY_WORDS = 50
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

SKIPPED_TEXT_LIMIT = "index text limit reached"
SKIPPED_TIME_LIMIT = "index build time limit reached"
PARTIAL_TEXT_LIMIT = "stopped at the index text limit"
# Left out for a limit of the whole index, not the file itself, so worth retrying.
_RETRY_DETAILS = (SKIPPED_TEXT_LIMIT, SKIPPED_TIME_LIMIT, PARTIAL_TEXT_LIMIT)

_SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE files(
    path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
    status TEXT NOT NULL, detail TEXT
);
CREATE TABLE sections_meta(id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL, location TEXT);
CREATE INDEX sections_meta_path ON sections_meta(path);
CREATE VIRTUAL TABLE sections USING fts5(
    text, content = '', tokenize = 'unicode61 remove_diacritics 2'
);
"""


# -------------------------------------------------------------------------- build


@dataclass
class BuildSummary:
    files_total: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    seconds: float = 0.0


@dataclass(frozen=True)
class FoundFile:
    path: Path
    size: int
    mtime_ns: int


def scan_files(root: Path, folder: Path, file_types: Sequence[str]) -> list[FoundFile]:
    """Every regular file under ``folder`` whose type is listed, cheapest first."""
    wanted = set(file_types)
    found: list[tuple[int, int, str, FoundFile]] = []
    for dirpath, _dirs, filenames in os.walk(folder, followlinks=False):
        for name in filenames:
            path = Path(dirpath) / name
            kind = file_type(path)
            if kind not in wanted or path.is_symlink():
                continue
            try:
                info = path.stat()
            except OSError:
                continue
            found.append(
                (
                    COST_ORDER[kind],
                    info.st_size,
                    str(path.relative_to(root)),
                    FoundFile(path, info.st_size, info.st_mtime_ns),
                )
            )
    found.sort(key=lambda entry: entry[:3])
    return [entry[3] for entry in found]


def collect_files(root: Path, folder: Path, file_types: Sequence[str]) -> list[Path]:
    """Every regular file under ``folder`` whose type is listed, cheapest first."""
    return [f.path for f in scan_files(root, folder, file_types)]


def _relative(path: Path, root: Path, prefix: str) -> str:
    rel = path.relative_to(root).as_posix()
    return f"{prefix.strip('/')}/{rel}" if prefix.strip("/") else rel


def _create(out_path: Path) -> sqlite3.Connection:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    for stale in (out_path, out_path.with_name(out_path.name + "-journal")):
        stale.unlink(missing_ok=True)
    # Owner-only: other apps in the sandbox (e.g. code execution) must not read it.
    os.close(os.open(out_path, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600))
    conn = sqlite3.connect(out_path, timeout=_BUSY_TIMEOUT_MS / 1000)
    conn.executescript(_SCHEMA)
    conn.executemany(
        "INSERT INTO meta(key, value) VALUES (?, ?)",
        [("format", FORMAT_NAME), ("version", FORMAT_VERSION), ("state", "building")],
    )
    return conn


def build_index(
    root: Path,
    out_path: Path,
    *,
    folder: Path | None = None,
    file_types: Sequence[str],
    path_prefix: str = "",
    built_by: str = "script",
    max_seconds_per_file: float = 30.0,
    max_total_chars: int = 500 * 1024 * 1024,
    max_build_seconds: float | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> BuildSummary:
    """Index every listed file type under ``folder`` (default ``root``) into ``out_path``.

    The file list is fixed when the build starts. A file that cannot be read is
    recorded with its reason and the build moves on; it never raises for one file.
    Past ``max_build_seconds`` the remaining files are recorded as skipped.
    """
    started = time.monotonic()
    deadline = started + max_build_seconds if max_build_seconds else None
    found = scan_files(root, folder or root, file_types)
    files = [f.path for f in found]
    conn = _create(out_path)
    summary = BuildSummary(files_total=len(files))
    try:
        conn.executemany(
            "INSERT INTO files(path, size, mtime_ns, status) VALUES (?, ?, ?, 'pending')",
            [(_relative(f.path, root, path_prefix), f.size, f.mtime_ns) for f in found],
        )
        conn.commit()

        chars_left = max_total_chars
        last_commit = time.monotonic()
        for done, path in enumerate(files, start=1):
            rel = _relative(path, root, path_prefix)
            if chars_left <= 0:
                status, detail = "skipped", SKIPPED_TEXT_LIMIT
            elif deadline is not None and time.monotonic() > deadline:
                status, detail = "skipped", SKIPPED_TIME_LIMIT
            else:
                status, detail, used = _index_file(
                    conn, path, rel, max_seconds_per_file, chars_left
                )
                chars_left -= used
            conn.execute(
                "UPDATE files SET status = ?, detail = ? WHERE path = ?",
                (status, detail, rel),
            )
            summary.counts[status] = summary.counts.get(status, 0) + 1
            if (
                done % _COMMIT_EVERY_FILES == 0
                or time.monotonic() - last_commit > _COMMIT_EVERY_SECONDS
            ):
                conn.commit()
                last_commit = time.monotonic()
            if on_progress:
                on_progress(done, len(files))

        conn.execute("UPDATE meta SET value = 'complete' WHERE key = 'state'")
        conn.executemany(
            "INSERT INTO meta(key, value) VALUES (?, ?)",
            [
                ("built_by", built_by),
                ("text_chars", str(max_total_chars - max(chars_left, 0))),
                ("build_seconds", f"{time.monotonic() - started:.1f}"),
                ("built_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
            ],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        conn.execute("UPDATE meta SET value = 'failed' WHERE key = 'state'")
        conn.commit()
        raise
    finally:
        conn.close()
    summary.seconds = time.monotonic() - started
    return summary


def _index_file(
    conn: sqlite3.Connection, path: Path, rel: str, max_seconds: float, max_chars: int
) -> tuple[str, str | None, int]:
    """Index one file; returns (status, detail, characters stored)."""
    used = 0
    status, detail = "indexed", None
    try:
        for section in extract_sections(
            path, max_seconds=max_seconds, max_chars=max_chars
        ):
            row_id = conn.execute(
                "INSERT INTO sections_meta(path, location) VALUES (?, ?)",
                (rel, section.location),
            ).lastrowid
            conn.execute(
                "INSERT INTO sections(rowid, text) VALUES (?, ?)",
                (row_id, section.text),
            )
            used += len(section.text)
    except TextLimitReached:
        limit = "index" if max_chars < MAX_CHARS_PER_FILE else "per-file"
        status, detail = "partial", f"stopped at the {limit} text limit"
    except TimeLimitReached:
        status, detail = "partial", f"stopped after the {max_seconds:g}s per-file limit"
    except NoTextError as exc:
        status, detail = "skipped", str(exc)
    except ExtractionError as exc:
        status, detail = "failed", str(exc)
    except Exception as exc:  # one bad file must not stop the build
        status, detail = "failed", f"{type(exc).__name__}: {exc}"
    if status == "indexed" and used == 0:
        status, detail = "skipped", "no text found"
    return status, detail, used


# ------------------------------------------------------------------------ refresh


@dataclass
class RefreshSummary:
    unchanged: int = 0
    added: int = 0
    updated: int = 0
    removed: int = 0
    pending: int = 0
    over_text_limit: int = 0
    seconds: float = 0.0


def _text_chars(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = 'text_chars'").fetchone()
    return int(row[0]) if row and row[0].isdigit() else 0


def _has_sections(conn: sqlite3.Connection, rel: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sections_meta WHERE path = ? LIMIT 1", (rel,)
        ).fetchone()
        is not None
    )


def _forget(conn: sqlite3.Connection, rel: str) -> None:
    conn.execute("DELETE FROM sections_meta WHERE path = ?", (rel,))
    conn.execute("DELETE FROM files WHERE path = ?", (rel,))


def _is_complete(conn: sqlite3.Connection) -> bool:
    try:
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    except sqlite3.DatabaseError:
        return False
    return (
        meta.get("format") == FORMAT_NAME
        and meta.get("version") == FORMAT_VERSION
        and meta.get("state") == "complete"
    )


def is_current_index(path: Path) -> bool:
    """True for a complete keyword index in the current format."""
    try:
        conn = _open_readonly(path)
    except sqlite3.Error:
        return False
    try:
        return _is_complete(conn)
    finally:
        conn.close()


def refresh_index(
    root: Path,
    index_paths: Sequence[Path],
    *,
    folder: Path | None = None,
    file_types: Sequence[str],
    max_seconds_per_file: float = 30.0,
    max_seconds: float = 15.0,
    max_total_chars: int = 500 * 1024 * 1024,
    trust_size: bool = False,
) -> RefreshSummary:
    """Bring complete indexes up to date with the files on disk now.

    Each file belongs to the first index that lists it; new files go into the first
    index. Changed files (by size, plus modified time unless ``trust_size``) are
    re-indexed cheapest first and removed files are dropped. ``trust_size`` is for
    an index built on another machine, whose modified times mean nothing here.
    Files left out earlier for a limit of the whole index are retried. Text stays
    within ``max_total_chars`` across all the indexes: past it, a file that is
    already indexed keeps its old entries and one that is not is recorded as skipped. Work stops after ``max_seconds``; leftovers are counted as ``pending``.
    """
    started = time.monotonic()
    summary = RefreshSummary()
    conns: list[sqlite3.Connection] = []
    try:
        for path in index_paths:
            conn = sqlite3.connect(path, timeout=_BUSY_TIMEOUT_MS / 1000)
            if _is_complete(conn):
                conns.append(conn)
            else:
                conn.close()
        if not conns:
            return summary
        chars_used = {id(conn): _text_chars(conn) for conn in conns}
        chars_left = max_total_chars - sum(chars_used.values())
        known: dict[str, tuple[sqlite3.Connection, int, int, str | None]] = {}
        for conn in conns:
            for path, size, mtime, detail in conn.execute(
                "SELECT path, size, mtime_ns, detail FROM files"
            ):
                if path in known:
                    _forget(conn, path)
                else:
                    known[path] = (conn, size, mtime, detail)
        on_disk = {
            _relative(f.path, root, ""): f
            for f in scan_files(root, folder or root, file_types)
        }
        for rel in known.keys() - on_disk.keys():
            _forget(known[rel][0], rel)
            summary.removed += 1
        for rel, found in on_disk.items():
            owner = known.get(rel)
            if owner is not None:
                conn, size, mtime, detail = owner
                same = size == found.size and (trust_size or mtime == found.mtime_ns)
                retry = detail in _RETRY_DETAILS and (
                    chars_left > 0 or detail == SKIPPED_TIME_LIMIT
                )
                if same and not retry:
                    if mtime != found.mtime_ns:
                        conn.execute(
                            "UPDATE files SET mtime_ns = ? WHERE path = ?",
                            (found.mtime_ns, rel),
                        )
                    summary.unchanged += 1
                    continue
            else:
                conn = conns[0]
            left = max_seconds - (time.monotonic() - started)
            if left <= 0:
                summary.pending += 1
                continue
            if chars_left <= 0 and owner is not None and _has_sections(conn, rel):
                summary.over_text_limit += 1
                continue
            _forget(conn, rel)
            if chars_left <= 0:
                status, detail = "skipped", SKIPPED_TEXT_LIMIT
                summary.over_text_limit += 1
            else:
                status, detail, used = _index_file(
                    conn,
                    found.path,
                    rel,
                    min(max_seconds_per_file, left),
                    min(MAX_CHARS_PER_FILE, chars_left),
                )
                chars_left -= used
                chars_used[id(conn)] += used
            conn.execute(
                "INSERT INTO files(path, size, mtime_ns, status, detail) VALUES (?, ?, ?, ?, ?)",
                (rel, found.size, found.mtime_ns, status, detail),
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('text_chars', ?)",
                (str(chars_used[id(conn)]),),
            )
            conn.commit()
            if owner is None:
                summary.added += 1
            else:
                summary.updated += 1
        for conn in conns:
            conn.commit()
    finally:
        for conn in conns:
            conn.close()
    summary.seconds = time.monotonic() - started
    return summary


# ------------------------------------------------------------------------- search


@dataclass(frozen=True)
class Hit:
    path: str
    location: str | None
    count: int
    score: float


@dataclass
class IndexStatus:
    name: str
    state: str
    counts: dict[str, int]
    unreadable: list[str]


@dataclass
class SearchOutcome:
    hits: list[Hit] = field(default_factory=list)
    total_sections: int = 0
    total_files: int = 0
    indexes: list[IndexStatus] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)


def find_index_files(*dirs: Path) -> list[Path]:
    found: list[Path] = []
    for folder in dirs:
        if folder.is_dir():
            found.extend(
                sorted(p for p in folder.rglob(f"*{INDEX_SUFFIX}") if p.is_file())
            )
    return found


def count_indexed_files(index_files: Sequence[Path]) -> int:
    """Files recorded across complete, current-format indexes; any other index counts 0."""
    total = 0
    for index_file in index_files:
        try:
            conn = _open_readonly(index_file)
        except sqlite3.Error:
            continue
        try:
            total += conn.execute(
                "SELECT count(*) FROM files WHERE"
                " (SELECT value FROM meta WHERE key = 'state') = 'complete'"
                " AND (SELECT value FROM meta WHERE key = 'version') = ?",
                (FORMAT_VERSION,),
            ).fetchone()[0]
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    return total


def _open_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(
        f"{path.as_uri()}?mode=ro", uri=True, timeout=_BUSY_TIMEOUT_MS / 1000
    )


def _read_status(
    conn: sqlite3.Connection,
    name: str,
    unreadable_limit: int,
    where: str = "",
    params: Sequence[str | int] = (),
) -> IndexStatus | None:
    """Index state plus file counts, limited to the same path filter as the search."""
    try:
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    except sqlite3.DatabaseError:
        return None
    if meta.get("format") != FORMAT_NAME or meta.get("version") != FORMAT_VERSION:
        return None
    counts = dict(
        conn.execute(
            f"SELECT status, count(*) FROM files WHERE 1{where} GROUP BY status",
            list(params),
        ).fetchall()
    )
    unreadable = [
        row[0]
        for row in conn.execute(
            f"SELECT path FROM files WHERE status IN ('failed', 'skipped'){where} "
            "ORDER BY path LIMIT ?",
            [*params, unreadable_limit],
        )
    ]
    return IndexStatus(
        name=name,
        state=meta.get("state", "complete"),
        counts=counts,
        unreadable=unreadable,
    )


def query_words(text: str) -> list[str]:
    """The distinct words of the agent's query, normalised like the index.

    Everything is a plain word: punctuation, quotes, ``OR`` and ``*`` have no
    special meaning, so the query can never be read as FTS5 syntax.
    """
    folded = unicodedata.normalize("NFKD", _CONTROL_RE.sub(" ", text).lower())
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    words = list(dict.fromkeys(_WORD_RE.findall(folded)))
    return words[:_MAX_QUERY_WORDS]


def _path_filter(prefix: str, column: str = "path") -> tuple[str, list[str | int]]:
    if not prefix:
        return "", []
    return f" AND ({column} = ? OR substr({column}, 1, ?) = ?)", [
        prefix,
        len(prefix) + 1,
        prefix + "/",
    ]


def search_indexes(
    index_files: Sequence[Path],
    words: Sequence[str],
    *,
    path_prefix: str = "",
    limit: int = 20,
    unreadable_limit: int = 5,
) -> SearchOutcome:
    """Pages with any of ``words``, best first (more occurrences rank higher)."""
    outcome = SearchOutcome()
    fts_query = " OR ".join(f'"{word}"' for word in words)
    status_where, status_params = _path_filter(path_prefix)
    where, params = _path_filter(path_prefix, "m.path")
    for index_file in index_files:
        try:
            conn = _open_readonly(index_file)
        except sqlite3.Error as exc:
            logger.warning(f"keyword index {index_file} cannot be opened: {exc}")
            outcome.ignored.append(index_file.name)
            continue
        try:
            status = _read_status(
                conn, index_file.name, unreadable_limit, status_where, status_params
            )
            if status is None:
                outcome.ignored.append(index_file.name)
                continue
            outcome.indexes.append(status)
            sections, files = conn.execute(
                "SELECT count(*), count(DISTINCT m.path) FROM sections "
                "JOIN sections_meta m ON m.id = sections.rowid "
                f"WHERE sections MATCH ?{where}",
                [fts_query, *params],
            ).fetchone()
            outcome.total_sections += sections
            outcome.total_files += files
            rows = conn.execute(
                "SELECT sections.rowid, m.path, m.location, bm25(sections) AS score "
                "FROM sections JOIN sections_meta m ON m.id = sections.rowid "
                f"WHERE sections MATCH ?{where} ORDER BY score LIMIT ?",
                [fts_query, *params, limit],
            ).fetchall()
            counts = _occurrences(conn, words, [r[0] for r in rows])
            outcome.hits.extend(
                Hit(path=r[1], location=r[2], count=counts.get(r[0], 0), score=r[3])
                for r in rows
            )
        except sqlite3.Error as exc:
            logger.warning(f"keyword index {index_file} could not be searched: {exc}")
            outcome.ignored.append(index_file.name)
        finally:
            conn.close()
    outcome.hits.sort(key=lambda hit: hit.score)
    del outcome.hits[limit:]
    return outcome


def _occurrences(
    conn: sqlite3.Connection, words: Sequence[str], row_ids: Sequence[int]
) -> dict[int, int]:
    """How many times the words occur in each of ``row_ids``."""
    if not row_ids:
        return {}
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS temp.sections_words "
        "USING fts5vocab(main, sections, instance)"
    )
    return dict(
        conn.execute(
            "SELECT doc, count(*) FROM temp.sections_words "
            f"WHERE term IN ({','.join('?' * len(words))}) "
            f"AND doc IN ({','.join('?' * len(row_ids))}) GROUP BY doc",
            [*words, *row_ids],
        ).fetchall()
    )
