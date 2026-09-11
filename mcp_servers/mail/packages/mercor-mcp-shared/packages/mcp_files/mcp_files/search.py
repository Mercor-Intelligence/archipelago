"""Search over a :class:`~mcp_files.vector_store.SqliteVectorStore`.

Three modes, all returning a ranked list of chunk-detail dicts:

* ``fts``    — FTS5 BM25 keyword search (always available).
* ``vector`` — sqlite-vec cosine/L2 KNN (needs an embedder + ``sqlite-vec``).
* ``hybrid`` — reciprocal-rank fusion (RRF) of the two (default).

All functions take the store so they reuse its connection, table prefix, and
embedder. Vector search returns an empty list when the vector backend is not
available rather than raising.
"""

from __future__ import annotations

import re
from typing import Any

from .vector_store import SqliteVectorStore, pack_embedding

RRF_K = 60

# Ranked hit: (chunk_id, score). Lower is better for both BM25 rank and vec
# distance; RRF only uses ordering, so the raw scales never need to align.
Hit = tuple[str, float]


def _fts_query_terms(query: str) -> str:
    """Turn free text into an OR of prefix-matched FTS5 terms."""
    tokens = re.findall(r"[\w@.-]+", query.lower())
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"*' for t in tokens)


def search_fts(store: SqliteVectorStore, query: str, limit: int = 10) -> list[Hit]:
    """BM25 keyword search; returns ``(chunk_id, rank)`` best-first."""
    match = _fts_query_terms(query)
    if not match:
        return []
    rows = store.conn.execute(
        f"""
        SELECT chunk_id, bm25({store.fts_table}) AS rank
        FROM {store.fts_table}
        WHERE {store.fts_table} MATCH ?
        ORDER BY rank
        LIMIT ?
        """,
        (match, limit),
    ).fetchall()
    return [(row[0], float(row[1])) for row in rows]


def search_vec(store: SqliteVectorStore, query: str, limit: int = 10) -> list[Hit]:
    """Semantic KNN search; empty when no embedder / sqlite-vec / vec table."""
    if not store.vector_enabled or store._encode is None or not store._has_vec_table():
        return []
    qvec = list(store._encode(query))
    # sqlite-vec requires an explicit ``k = ?`` KNN constraint here: the vec0
    # virtual table cannot infer the neighbour count from the statement's outer
    # ``LIMIT`` once another table is JOINed in (raises "A LIMIT or 'k = ?'
    # constraint is required on vec0 knn queries"). ``k = ?`` is the portable form.
    rows = store.conn.execute(
        f"""
        SELECT c.chunk_id, v.distance
        FROM {store.vec_table} v
        JOIN {store.chunks_table} c ON c.rowid = v.rowid
        WHERE v.embedding MATCH ? AND k = ?
        ORDER BY v.distance
        """,
        (pack_embedding(qvec), limit),
    ).fetchall()
    return [(row[0], float(row[1])) for row in rows]


def rrf_merge(*ranked_lists: list[Hit], k: int = RRF_K) -> list[Hit]:
    """Reciprocal-rank fusion of several ranked lists (higher score = better)."""
    scores: dict[str, float] = {}
    for lst in ranked_lists:
        for rank, (chunk_id, _) in enumerate(lst):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)


def fetch_chunks(store: SqliteVectorStore, chunk_ids: list[str]) -> list[dict[str, Any]]:
    """Hydrate ``chunk_ids`` into detail dicts (chunk text + document metadata)."""
    if not chunk_ids:
        return []
    placeholders = ",".join("?" * len(chunk_ids))
    rows = store.conn.execute(
        f"""
        SELECT c.chunk_id, c.doc_id, c.chunk_index, c.text,
               d.filename, d.mime_type, d.source_ref_json
        FROM {store.chunks_table} c
        JOIN {store.documents_table} d ON d.doc_id = c.doc_id
        WHERE c.chunk_id IN ({placeholders})
        """,
        chunk_ids,
    ).fetchall()
    by_id = {
        row[0]: {
            "chunk_id": row[0],
            "doc_id": row[1],
            "chunk_index": row[2],
            "text": row[3],
            "filename": row[4],
            "mime_type": row[5],
            "source_ref_json": row[6],
        }
        for row in rows
    }
    return [by_id[cid] for cid in chunk_ids if cid in by_id]


def search(
    store: SqliteVectorStore,
    query: str,
    *,
    mode: str = "hybrid",
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Run ``query`` in ``fts`` / ``vector`` / ``hybrid`` mode; return chunk dicts."""
    fts_hits = search_fts(store, query, limit) if mode in {"fts", "hybrid"} else []
    vec_hits = search_vec(store, query, limit) if mode in {"vector", "hybrid"} else []

    if mode == "fts":
        ranked = fts_hits
    elif mode == "vector":
        ranked = vec_hits
    else:
        ranked = rrf_merge(fts_hits, vec_hits)

    chunk_ids = [cid for cid, _ in ranked[:limit]]
    return fetch_chunks(store, chunk_ids)
