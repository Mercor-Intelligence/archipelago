"""CLI for generic file populate / snapshot / index / search.

Examples::

    # Populate a filesystem store from a folder (recursive) and a CSV manifest
    mcp-files populate ./drop/files.csv ./extra_docs --out ./corpus -r

    # Snapshot a populated store back to a portable files drop
    mcp-files snapshot --store ./corpus --out ./snapshot

    # Index files (FTS5, optionally sqlite-vec) into a SQLite database
    mcp-files index ./drop/files.csv ./docs --database ./app.db -r

    # Search the index (fts / vector / hybrid)
    mcp-files search "quarterly budget" --database ./app.db --mode fts
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .importer import import_files
from .pipeline import populate_files, snapshot_files
from .search import search as run_search
from .store import FilesystemFileStore
from .vector_store import SqliteVectorStore


def _cmd_populate(args: argparse.Namespace) -> int:
    store = FilesystemFileStore(Path(args.out), store_blobs=not args.no_blobs)
    stats = populate_files(
        args.inputs,
        store,
        files_root=Path(args.files_root) if args.files_root else None,
        recursive=args.recursive,
        extract=not args.no_extract,
        retain_bytes=not args.no_blobs,
        follow_archives=args.follow_archives,
    )
    print(
        f"populate: imported={stats.imported} extracted_ok={stats.extracted_ok} "
        f"skipped={stats.skipped} store={args.out}"
    )
    return 0


def _cmd_snapshot(args: argparse.Namespace) -> int:
    store = FilesystemFileStore(Path(args.store))
    stats = snapshot_files(store, Path(args.out))
    print(f"snapshot: files={stats.files} blobs={stats.blobs} out={args.out}")
    return 0


def _build_embedder(model_name: str | None):
    """Return a sentence-transformers ``.encode`` callable, or ``None``."""
    if not model_name:
        return None
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise SystemExit(
            "Vector indexing needs sentence-transformers: pip install "
            "'mcp-files[embeddings]' (or run without --embed-model)"
        ) from exc
    model = SentenceTransformer(model_name)
    return lambda text: model.encode(text, normalize_embeddings=True).tolist()


def _open_store(args: argparse.Namespace) -> SqliteVectorStore:
    return SqliteVectorStore(
        Path(args.database),
        table_prefix=args.table_prefix,
        embedder=_build_embedder(getattr(args, "embed_model", None)),
    )


def _cmd_index(args: argparse.Namespace) -> int:
    store = _open_store(args)
    if args.embed_model and not store.vector_enabled:
        print("warning: sqlite-vec unavailable — indexing FTS5 keyword only")
    indexed = 0
    for imported in import_files(
        args.inputs,
        files_root=Path(args.files_root) if args.files_root else None,
        recursive=args.recursive,
        follow_archives=args.follow_archives,
    ):
        store.upsert(imported)
        indexed += 1
    print(
        f"index: documents={indexed} total={store.count_documents()} "
        f"vector={'on' if store.vector_enabled else 'off'} db={args.database}"
    )
    store.close()
    return 0


def _cmd_search(args: argparse.Namespace) -> int:
    store = _open_store(args)
    results = run_search(store, args.query, mode=args.mode, limit=args.limit)
    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        for i, row in enumerate(results, 1):
            snippet = (row["text"] or "")[:240].replace("\n", " ")
            print(f"{i}. [{row['filename']}] chunk {row['chunk_index']} ({row['doc_id']})")
            print(f"   {snippet}\n")
    store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(prog="mcp-files", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    pop = sub.add_parser("populate", help="Import files (dir / CSV manifest / inline) into a store")
    pop.add_argument("inputs", nargs="+", help="Files, directories, or CSV manifests")
    pop.add_argument("--out", "-o", default="./corpus", help="Filesystem store root")
    pop.add_argument("--files-root", help="Base dir for relative CSV manifest paths")
    pop.add_argument("--recursive", "-r", action="store_true", help="Recurse into directories")
    pop.add_argument("--no-extract", action="store_true", help="Skip text extraction")
    pop.add_argument("--no-blobs", action="store_true", help="Do not retain raw bytes")
    pop.add_argument(
        "--follow-archives",
        action="store_true",
        help="Also import files nested inside zip/tar/gz archives",
    )
    pop.set_defaults(func=_cmd_populate)

    snap = sub.add_parser("snapshot", help="Export a store to a portable files drop")
    snap.add_argument("--store", required=True, help="Filesystem store root to export")
    snap.add_argument("--out", "-o", required=True, help="Output directory for the snapshot")
    snap.set_defaults(func=_cmd_snapshot)

    idx = sub.add_parser("index", help="Index files (FTS5 + optional vectors) into a SQLite DB")
    idx.add_argument("inputs", nargs="+", help="Files, directories, or CSV manifests")
    idx.add_argument("--database", "-d", required=True, help="Target SQLite database path")
    idx.add_argument("--files-root", help="Base dir for relative CSV manifest paths")
    idx.add_argument("--recursive", "-r", action="store_true", help="Recurse into directories")
    idx.add_argument(
        "--follow-archives",
        action="store_true",
        help="Also index files nested inside zip/tar/gz archives",
    )
    idx.add_argument("--table-prefix", default="mcpfiles_", help="Prefix for index tables")
    idx.add_argument(
        "--embed-model",
        help="sentence-transformers model name to enable semantic vectors",
    )
    idx.set_defaults(func=_cmd_index)

    sea = sub.add_parser("search", help="Search an indexed SQLite database")
    sea.add_argument("query", help="Free-text query")
    sea.add_argument("--database", "-d", required=True, help="SQLite database path to search")
    sea.add_argument("--mode", choices=("fts", "vector", "hybrid"), default="hybrid")
    sea.add_argument("--limit", type=int, default=10)
    sea.add_argument("--table-prefix", default="mcpfiles_", help="Prefix for index tables")
    sea.add_argument("--embed-model", help="sentence-transformers model for vector/hybrid")
    sea.add_argument("--json", action="store_true", help="Emit JSON results")
    sea.set_defaults(func=_cmd_search)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
