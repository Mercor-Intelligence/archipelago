"""SQLite-backed vector / keyword index that lives in an *existing* database.

:class:`SqliteVectorStore` is a :class:`~mcp_files.store.FileStore` that chunks
each imported file's extracted text and indexes it for keyword (FTS5) and,
optionally, semantic (sqlite-vec ``vec0``) search. Unlike a standalone corpus
indexer it targets a caller-supplied ``sqlite3.Connection`` or DB path and keeps
all of its tables behind a configurable ``table_prefix``, so the index can sit
*inside* an app's main database alongside its own tables.

Both backends degrade gracefully:

* FTS5 is part of stock SQLite; keyword indexing always works.
* ``vec0`` requires the optional ``sqlite-vec`` extension *and* an embedder.
  When either is absent the vector table is simply not created and semantic
  search returns nothing rather than raising.
"""

from __future__ import annotations

import json
import sqlite3
import struct
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .chunking import DEFAULT_CHUNK_CHARS, DEFAULT_CHUNK_OVERLAP, chunk_text
from .models import ExtractedContent, FileMetadata, ImportedFile, SourceRef

# FTS5 tokenizer: fold diacritics and keep ``_`` / ``-`` so identifiers and
# email-style tokens stay searchable.
FTS_TOKENIZE = "unicode61 remove_diacritics 2 tokenchars '_-'"

# An embedder is anything callable ``str -> sequence[float]`` (or an object
# exposing ``.encode(str)``, e.g. a sentence-transformers model wrapper).
Embedder = Callable[[str], Sequence[float]]


def load_sqlite_vec(conn: sqlite3.Connection) -> bool:
    """Load the ``sqlite-vec`` extension into ``conn``; return success.

    Returns ``False`` (never raises) when the package is not installed or the
    SQLite build disallows loadable extensions.
    """
    try:
        import sqlite_vec
    except ImportError:
        return False
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except (AttributeError, sqlite3.OperationalError):
        return False
    return True


def pack_embedding(vec: Sequence[float]) -> bytes:
    """Pack a float vector into the little-endian ``float32`` blob vec0 wants."""
    return struct.pack(f"{len(vec)}f", *vec)


def _normalize_embedder(embedder: Embedder | Any | None) -> Embedder | None:
    """Accept a plain callable or an object with ``.encode``; return a callable."""
    if embedder is None:
        return None
    if hasattr(embedder, "encode"):
        return lambda text: embedder.encode(text)  # type: ignore[union-attr]
    return embedder


class SqliteVectorStore:
    """A :class:`~mcp_files.store.FileStore` writing into an existing SQLite DB.

    Args:
        target: An open ``sqlite3.Connection`` (borrowed, not closed by us) or a
            path / URL to a SQLite file (opened and owned by this store).
        table_prefix: Prefix for the four managed tables (default ``mcpfiles_``):
            ``<prefix>documents`` / ``<prefix>chunks`` / ``<prefix>chunks_fts`` /
            ``<prefix>chunks_vec``.
        embedder: Optional ``str -> vector`` callable (or object with ``.encode``)
            enabling semantic indexing when ``sqlite-vec`` is also available.
        embed_dim: Embedding dimensionality. Inferred from ``embedder`` when
            omitted.
        chunk_chars / chunk_overlap: Chunking knobs (see :mod:`mcp_files.chunking`).
        create: Create the managed tables on init (default ``True``).
    """

    def __init__(
        self,
        target: sqlite3.Connection | str | Path,
        *,
        table_prefix: str = "mcpfiles_",
        embedder: Embedder | Any | None = None,
        embed_dim: int | None = None,
        chunk_chars: int = DEFAULT_CHUNK_CHARS,
        chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
        create: bool = True,
        commit_every: int = 1,
    ) -> None:
        if isinstance(target, sqlite3.Connection):
            self.conn = target
            self._owns_conn = False
        else:
            path = str(target)
            db_path = path.split("sqlite:///")[-1] if path.startswith("sqlite") else path
            self.conn = sqlite3.connect(db_path)
            self._owns_conn = True

        self.prefix = table_prefix
        self.chunk_chars = chunk_chars
        self.chunk_overlap = chunk_overlap
        self._encode = _normalize_embedder(embedder)
        self._commit_every = max(1, commit_every)
        self._pending_commits = 0

        self._vec_enabled = False
        self._embed_dim = embed_dim
        if self._encode is not None:
            if self._embed_dim is None:
                self._embed_dim = len(list(self._encode("probe")))
            self._vec_enabled = load_sqlite_vec(self.conn)

        if create:
            self._ensure_schema()

    # -- table names --------------------------------------------------------
    @property
    def documents_table(self) -> str:
        return f"{self.prefix}documents"

    @property
    def chunks_table(self) -> str:
        return f"{self.prefix}chunks"

    @property
    def fts_table(self) -> str:
        return f"{self.prefix}chunks_fts"

    @property
    def vec_table(self) -> str:
        return f"{self.prefix}chunks_vec"

    @property
    def chunks_doc_index(self) -> str:
        """Name of the ``chunks(doc_id)`` secondary index (see ``_ensure_schema``)."""
        return f"idx_{self.prefix}chunks_doc"

    @property
    def vector_enabled(self) -> bool:
        """True when semantic indexing is active (embedder + sqlite-vec ready)."""
        return self._vec_enabled and self._encode is not None

    # -- schema -------------------------------------------------------------
    def _ensure_schema(self) -> None:
        cur = self.conn
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.documents_table} (
                doc_id TEXT PRIMARY KEY,
                filename TEXT,
                mime_type TEXT,
                extension TEXT,
                source_app TEXT,
                source_ref_json TEXT,
                full_text TEXT,
                extract_status TEXT,
                extract_method TEXT,
                warnings_json TEXT,
                indexed_at TEXT,
                size_bytes INTEGER
            )
            """
        )
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.chunks_table} (
                rowid INTEGER PRIMARY KEY AUTOINCREMENT,
                chunk_id TEXT NOT NULL UNIQUE,
                doc_id TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                text TEXT NOT NULL,
                char_count INTEGER,
                meta_json TEXT
            )
            """
        )
        cur.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{self.prefix}chunks_doc "
            f"ON {self.chunks_table} (doc_id)"
        )
        cur.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {self.fts_table} USING fts5("
            f'chunk_id UNINDEXED, doc_id UNINDEXED, text, tokenize="{FTS_TOKENIZE}")'
        )
        if self.vector_enabled:
            cur.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {self.vec_table} USING vec0("
                f"embedding float[{self._embed_dim}])"
            )
        # Migrate pre-existing tables that lack the size_bytes column (added in 0.2.0).
        # ALTER TABLE … ADD COLUMN is idempotent-safe: we catch the OperationalError
        # SQLite raises when the column already exists rather than using IF NOT EXISTS
        # (which SQLite does not support for ADD COLUMN).
        try:
            self.conn.execute(f"ALTER TABLE {self.documents_table} ADD COLUMN size_bytes INTEGER")
        except sqlite3.OperationalError:
            pass  # column already exists
        self.conn.commit()

    def _has_vec_table(self) -> bool:
        row = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (self.vec_table,),
        ).fetchone()
        return row is not None

    # -- FileStore protocol -------------------------------------------------
    def upsert(self, imported: ImportedFile, *, fresh: bool = False) -> None:
        """Index ``imported`` (document row + chunks + FTS + optional vectors).

        Args:
            imported: The document to index.
            fresh: Caller promise that this ``doc_id`` has never been indexed in
                this store (e.g. a full build into freshly-created tables). When
                ``True`` the per-document SELECT+DELETE "replace prior chunks"
                probe is skipped — a large saving across a big corpus where that
                probe returns nothing on every call. Leave ``False`` (default)
                for incremental / re-ingest writes; passing ``True`` against a
                ``doc_id`` that already has rows raises a ``chunk_id`` UNIQUE
                violation.
        """
        doc_id = imported.file_id
        meta = imported.metadata
        content = imported.content
        indexed_at = datetime.now(UTC).isoformat()

        self.conn.execute(
            f"""
            INSERT INTO {self.documents_table} (
                doc_id, filename, mime_type, extension, source_app, source_ref_json,
                full_text, extract_status, extract_method, warnings_json, indexed_at,
                size_bytes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(doc_id) DO UPDATE SET
                filename=excluded.filename,
                mime_type=excluded.mime_type,
                extension=excluded.extension,
                source_app=excluded.source_app,
                source_ref_json=excluded.source_ref_json,
                full_text=excluded.full_text,
                extract_status=excluded.extract_status,
                extract_method=excluded.extract_method,
                warnings_json=excluded.warnings_json,
                indexed_at=excluded.indexed_at,
                size_bytes=excluded.size_bytes
            """,
            (
                doc_id,
                meta.filename,
                meta.mime_type,
                meta.extension,
                meta.source_app,
                json.dumps({"path": imported.source.path, **imported.source.extra}),
                content.text,
                content.status,
                content.method,
                json.dumps(content.warnings),
                indexed_at,
                meta.size_bytes,
            ),
        )

        # Replace any prior chunks for this document (content-addressed re-ingest).
        # ``fresh`` callers skip this probe — see the docstring; on a full build
        # into empty tables it would return nothing on every call.
        has_vec = self.vector_enabled and self._has_vec_table()
        if not fresh:
            old = self.conn.execute(
                f"SELECT rowid, chunk_id FROM {self.chunks_table} WHERE doc_id = ?",
                (doc_id,),
            ).fetchall()
            for rowid, chunk_id in old:
                self.conn.execute(f"DELETE FROM {self.fts_table} WHERE chunk_id = ?", (chunk_id,))
                if has_vec:
                    self.conn.execute(f"DELETE FROM {self.vec_table} WHERE rowid = ?", (rowid,))
            self.conn.execute(f"DELETE FROM {self.chunks_table} WHERE doc_id = ?", (doc_id,))

        chunks = chunk_text(
            content.text,
            doc_id=doc_id,
            chunk_chars=self.chunk_chars,
            overlap=self.chunk_overlap,
        )
        if chunks:
            # Batch the chunk / FTS / vec writes into three executemany() calls
            # instead of 3×N execute()s. The vec0 table is keyed by rowid and the
            # search path joins ``chunks.rowid = vec.rowid``, so the two must share
            # ids; when batching we can't read back per-row ``lastrowid``, so we
            # reserve a contiguous block at ``MAX(rowid)+1`` and reuse those ids
            # for the chunk rows and their embeddings. Explicit rowids are valid
            # for the AUTOINCREMENT PK and keep the sequence monotonic.
            base = self.conn.execute(
                f"SELECT COALESCE(MAX(rowid), 0) FROM {self.chunks_table}"
            ).fetchone()[0]
            chunk_rows: list[tuple[Any, ...]] = []
            fts_rows: list[tuple[Any, ...]] = []
            vec_rows: list[tuple[Any, ...]] = []
            for offset, chunk in enumerate(chunks, start=1):
                rowid = base + offset
                chunk_rows.append(
                    (
                        rowid,
                        chunk.chunk_id,
                        chunk.doc_id,
                        chunk.index,
                        chunk.text,
                        chunk.char_count,
                        json.dumps(chunk.meta),
                    )
                )
                fts_rows.append((chunk.chunk_id, chunk.doc_id, chunk.text))
                if has_vec and self._encode is not None:
                    vec = list(self._encode(chunk.text))
                    if len(vec) != self._embed_dim:
                        raise ValueError(f"Embedding dim {len(vec)} != expected {self._embed_dim}")
                    vec_rows.append((rowid, pack_embedding(vec)))
            self.conn.executemany(
                f"INSERT INTO {self.chunks_table} "
                f"(rowid, chunk_id, doc_id, chunk_index, text, char_count, meta_json) "
                f"VALUES (?, ?, ?, ?, ?, ?, ?)",
                chunk_rows,
            )
            self.conn.executemany(
                f"INSERT INTO {self.fts_table} (chunk_id, doc_id, text) VALUES (?, ?, ?)",
                fts_rows,
            )
            if vec_rows:
                self.conn.executemany(
                    f"INSERT INTO {self.vec_table}(rowid, embedding) VALUES (?, ?)",
                    vec_rows,
                )
        self._pending_commits += 1
        if self._pending_commits >= self._commit_every:
            self.conn.commit()
            self._pending_commits = 0

    def flush(self) -> None:
        """Commit any pending upserts that have not yet been written.

        Call this after a batch upsert loop when ``commit_every > 1`` to
        ensure the final partial batch is persisted.
        """
        if self._pending_commits > 0:
            self.conn.commit()
            self._pending_commits = 0

    def drop_chunk_doc_index(self) -> None:
        """Drop the ``chunks(doc_id)`` secondary index for a bulk load.

        During a fresh full build the lookup index is dead weight — maintaining
        its b-tree across every chunk insert costs more than building it once at
        the end. Drop it before the load, then call
        :meth:`create_chunk_doc_index` after. Safe no-op if absent. Only sound
        when the load uses ``upsert(..., fresh=True)`` (which never issues the
        per-doc ``WHERE doc_id = ?`` lookup that relies on this index).
        """
        self.conn.execute(f"DROP INDEX IF EXISTS {self.chunks_doc_index}")

    def create_chunk_doc_index(self) -> None:
        """(Re)create the ``chunks(doc_id)`` secondary index after a bulk load."""
        self.conn.execute(
            f"CREATE INDEX IF NOT EXISTS {self.chunks_doc_index} ON {self.chunks_table} (doc_id)"
        )

    def iter_files(self) -> Iterator[ImportedFile]:
        """Reconstruct stored documents as :class:`ImportedFile` (no raw bytes)."""
        rows = self.conn.execute(
            f"""
            SELECT doc_id, filename, mime_type, extension, source_app,
                   source_ref_json, full_text, extract_status, extract_method,
                   warnings_json, size_bytes
            FROM {self.documents_table}
            """
        ).fetchall()
        for row in rows:
            source_ref = json.loads(row[5] or "{}")
            path = source_ref.pop("path", "")
            yield ImportedFile(
                file_id=row[0],
                source=SourceRef(
                    filename=row[1] or "",
                    mime_type=row[2] or "",
                    path=path,
                    source_app=row[4] or "filesystem",
                    extra=source_ref,
                ),
                metadata=FileMetadata(
                    filename=row[1] or "",
                    extension=row[3] or "",
                    mime_type=row[2] or "",
                    size_bytes=row[10] or 0,
                    sha256=row[0].split(":", 1)[-1],
                    source_app=row[4] or "filesystem",
                ),
                content=ExtractedContent(
                    text=row[6] or "",
                    method=row[8] or "",
                    status=row[7] or "",
                    warnings=json.loads(row[9] or "[]"),
                ),
                data=None,
            )

    def read_bytes(self, file_id: str) -> bytes | None:
        """A vector index does not retain raw bytes; always ``None``."""
        return None

    # -- lifecycle ----------------------------------------------------------
    def count_documents(self) -> int:
        row = self.conn.execute(f"SELECT COUNT(*) FROM {self.documents_table}").fetchone()
        return int(row[0]) if row else 0

    def close(self) -> None:
        """Flush pending upserts and close the connection (owned connections only)."""
        self.flush()
        if self._owns_conn:
            self.conn.close()
