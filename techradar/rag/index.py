"""Vector index: one Chroma collection per KB bucket.

Bucket scoping is an *index-level pre-filter by construction*: a regulatory
query opens only the ``kb_regulatory`` collection and never touches the
research index. At 50k documents this is the difference between scanning a
~45k-vector ANN index and a ~15k one — and it removes the failure mode where a
metadata post-filter silently returns fewer than k results.

Date filtering inside a bucket uses a numeric ``published_ts`` (yyyymmdd int)
metadata field so range operators apply at query time.
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import BUCKETS, CHROMA_DIR
from .. import db
from ..schema import utcnow
from .chunking import chunk_document
from .embedding import embed_passages

log = logging.getLogger("techradar.rag")


def _client():
    import chromadb
    CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(path=str(CHROMA_DIR))


def collection(bucket: str, client=None):
    client = client or _client()
    return client.get_or_create_collection(
        f"kb_{bucket}", metadata={"hnsw:space": "cosine"}
    )


def _published_ts(published_at: str | None) -> int:
    if not published_at or len(published_at) < 10:
        return 0
    try:
        return int(published_at[:10].replace("-", ""))
    except ValueError:
        return 0


def build_index(batch_size: int = 256, limit: int | None = None) -> dict[str, Any]:
    """Chunk any un-chunked documents, then embed + index any un-embedded chunks.

    Incremental and resumable: progress is tracked in SQLite (``chunks`` rows,
    ``embedded_at`` stamps), so interrupting and re-running continues where it
    stopped, and updated documents (whose chunks were invalidated) re-index.
    """
    conn = db.connect()
    stats = {"docs_chunked": 0, "chunks_created": 0, "chunks_indexed": 0}

    # 1. chunk documents that have no chunks yet (canonical, non-stale-agnostic:
    #    stale docs stay retrievable — exclusion is a query-time choice)
    q = (
        "SELECT d.* FROM documents d LEFT JOIN chunks c ON c.doc_id = d.doc_id "
        "WHERE c.doc_id IS NULL AND d.duplicate_of IS NULL AND d.storage_mode != 'link_only'"
    )
    if limit:
        q += f" LIMIT {int(limit)}"
    rows = [dict(r) for r in conn.execute(q)]
    with db.transaction(conn):
        for doc in rows:
            pieces = chunk_document(doc)
            for seq, text in enumerate(pieces):
                conn.execute(
                    "INSERT OR IGNORE INTO chunks (chunk_id, doc_id, seq, text, bucket, "
                    "sub_bucket, source, published_at) VALUES (?,?,?,?,?,?,?,?)",
                    (f"{doc['doc_id']}#{seq}", doc["doc_id"], seq, text,
                     doc["bucket"], doc["sub_bucket"], doc["source"], doc["published_at"]),
                )
                conn.execute(
                    "INSERT INTO chunks_fts (text, chunk_id, bucket) VALUES (?,?,?)",
                    (text, f"{doc['doc_id']}#{seq}", doc["bucket"]),
                )
                stats["chunks_created"] += 1
            stats["docs_chunked"] += 1

    # 2. embed + index pending chunks, bucket by bucket
    client = _client()
    for bucket in BUCKETS:
        col = collection(bucket, client)
        while True:
            pending = conn.execute(
                "SELECT chunk_id, doc_id, text, sub_bucket, source, published_at "
                "FROM chunks WHERE embedded_at IS NULL AND bucket = ? LIMIT ?",
                (bucket, batch_size * 4),
            ).fetchall()
            if not pending:
                break
            texts = [r["text"] for r in pending]
            vectors = list(embed_passages(texts, batch_size=batch_size))
            col.upsert(
                ids=[r["chunk_id"] for r in pending],
                embeddings=vectors,
                metadatas=[{
                    "doc_id": r["doc_id"],
                    "sub_bucket": r["sub_bucket"],
                    "source": r["source"],
                    "published_ts": _published_ts(r["published_at"]),
                } for r in pending],
            )
            now = utcnow()
            conn.executemany(
                "UPDATE chunks SET embedded_at = ? WHERE chunk_id = ?",
                [(now, r["chunk_id"]) for r in pending],
            )
            conn.commit()
            stats["chunks_indexed"] += len(pending)
            log.info("index: %s +%d (total this run %d)",
                     bucket, len(pending), stats["chunks_indexed"])
    conn.close()
    return stats


def index_counts() -> dict[str, int]:
    client = _client()
    return {b: collection(b, client).count() for b in BUCKETS}
