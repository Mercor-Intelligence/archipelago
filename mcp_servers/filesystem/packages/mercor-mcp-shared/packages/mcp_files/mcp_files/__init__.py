"""mcp_files — generic file import, metadata, content extraction, populate/snapshot.

App-agnostic building blocks reused across MCP servers (Gmail, Drive, Zoho,
Teams, …) so each app does not re-implement attachment/document handling:

* **import**   — :func:`import_files` from directories, CSV manifests
  (with sibling ``files/`` resolution), or inline base64.
* **metadata** — :func:`extract_metadata` (content-addressed id, size, MIME).
* **content**  — :func:`extract_content` (PDF/Office/text/ics/eml/image/archive).
* **store**    — :class:`FileStore` protocol + in-memory / filesystem refs.
* **populate** — :func:`populate_files` into any store.
* **snapshot** — :func:`snapshot_files` to a portable, round-trippable drop.

Quick start::

    from mcp_files import populate_files, snapshot_files, FilesystemFileStore

    store = FilesystemFileStore("./corpus")
    populate_files(["./drop/files.csv"], store)
    snapshot_files(store, "./snapshot")
"""

from .chunking import (
    DEFAULT_CHUNK_CHARS,
    DEFAULT_CHUNK_OVERLAP,
    ChunkRecord,
    chunk_text,
)
from .extract import MAX_ARCHIVE_DEPTH, MAX_EXTRACTED_BYTES, extract_content
from .importer import (
    DEFAULT_MAX_ARCHIVE_DEPTH,
    import_file,
    import_files,
    iter_csv_manifest,
    iter_directory,
    iter_nested_files,
    iter_sources,
    read_source_bytes,
)
from .metadata import compute_sha256, extract_metadata, file_id_for
from .mime import extension, guess_mime, resolve_mime, sniff_office_zip
from .models import (
    STATUS_EMPTY,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_SKIPPED,
    ExtractedContent,
    FileMetadata,
    ImportedFile,
    SourceRef,
)
from .pipeline import (
    PopulateStats,
    SnapshotStats,
    populate_files,
    snapshot_files,
)
from .search import fetch_chunks, rrf_merge, search, search_fts, search_vec
from .store import FileStore, FilesystemFileStore, InMemoryFileStore
from .vector_store import SqliteVectorStore, load_sqlite_vec, pack_embedding

__version__ = "0.2.0"

__all__ = [
    # Models
    "SourceRef",
    "FileMetadata",
    "ExtractedContent",
    "ImportedFile",
    "STATUS_OK",
    "STATUS_EMPTY",
    "STATUS_FAILED",
    "STATUS_SKIPPED",
    # MIME
    "guess_mime",
    "resolve_mime",
    "sniff_office_zip",
    "extension",
    # Metadata
    "extract_metadata",
    "compute_sha256",
    "file_id_for",
    # Content
    "extract_content",
    "MAX_EXTRACTED_BYTES",
    "MAX_ARCHIVE_DEPTH",
    # Import
    "import_file",
    "import_files",
    "iter_sources",
    "iter_directory",
    "iter_csv_manifest",
    "iter_nested_files",
    "read_source_bytes",
    "DEFAULT_MAX_ARCHIVE_DEPTH",
    # Chunking
    "chunk_text",
    "ChunkRecord",
    "DEFAULT_CHUNK_CHARS",
    "DEFAULT_CHUNK_OVERLAP",
    # Store
    "FileStore",
    "InMemoryFileStore",
    "FilesystemFileStore",
    "SqliteVectorStore",
    "load_sqlite_vec",
    "pack_embedding",
    # Search
    "search",
    "search_fts",
    "search_vec",
    "rrf_merge",
    "fetch_chunks",
    # Pipeline
    "populate_files",
    "snapshot_files",
    "PopulateStats",
    "SnapshotStats",
]
