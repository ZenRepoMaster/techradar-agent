"""Hybrid retrieval: dense (per-bucket ANN) + keyword (FTS5), fused with RRF.

Context assembly hydrates results from SQLite — the authoritative store for
staleness — so a document flagged stale *after* it was indexed is still
surfaced/excluded correctly at query time. Every result carries its staleness
flag, source, and bucket so downstream consumers (MCP tool, agent) can render
provenance instead of bare text.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from ..config import BUCKETS
from .. import db
from .embedding import embed_query
from .index import _client, collection, _published_ts

RRF_K = 60  # standard reciprocal-rank-fusion constant


@dataclass
class SearchResult:
    chunk_id: str
    doc_id: str
    title: str
    text: str
    score: float
    bucket: str
    sub_bucket: str
    source: str
    canonical_url: str
    published_at: str | None
    is_stale: bool
    stale_reason: str | None
    doc_type: str
    legs: list[str] = field(default_factory=list)  # which retrieval legs hit

    def context_block(self) -> str:
        """Rendered form used in agent prompts: text + provenance + flags."""
        flags = " [STALE: %s]" % (self.stale_reason or "outdated") if self.is_stale else ""
        date = f", {self.published_at}" if self.published_at else ""
        return (f"[{self.doc_id}] ({self.bucket}/{self.source}{date}){flags}\n"
                f"{self.text}\nURL: {self.canonical_url}")


class Retriever:
    def __init__(self, conn: sqlite3.Connection | None = None):
        self.conn = conn or db.connect()
        self._chroma = _client()

    def search(self, query: str, buckets: list[str] | None = None, k: int = 8,
               include_stale: bool = True, date_from: str | None = None,
               date_to: str | None = None,
               sub_buckets: list[str] | None = None) -> list[SearchResult]:
        buckets = [b for b in (buckets or list(BUCKETS)) if b in BUCKETS]
        fetch_n = max(k * 3, 20)

        dense = self._dense(query, buckets, fetch_n, date_from, date_to, sub_buckets)
        keyword = self._keyword(query, buckets, fetch_n)

        fused: dict[str, float] = {}
        legs: dict[str, list[str]] = {}
        for name, ranking in (("dense", dense), ("keyword", keyword)):
            for rank, chunk_id in enumerate(ranking):
                fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank + 1)
                legs.setdefault(chunk_id, []).append(name)

        ordered = sorted(fused, key=fused.get, reverse=True)
        results: list[SearchResult] = []
        seen_docs: set[str] = set()
        for chunk_id in ordered:
            r = self._hydrate(chunk_id, fused[chunk_id], legs[chunk_id])
            if r is None:
                continue
            if not include_stale and r.is_stale:
                continue
            if date_from and (r.published_at or "") < date_from:
                continue
            if date_to and (r.published_at or "9999") > date_to:
                continue
            if sub_buckets and r.sub_bucket not in sub_buckets:
                continue
            if r.doc_id in seen_docs:  # one chunk per document in final context
                continue
            seen_docs.add(r.doc_id)
            results.append(r)
            if len(results) >= k:
                break
        return results

    def _dense(self, query: str, buckets: list[str], n: int,
               date_from: str | None, date_to: str | None,
               sub_buckets: list[str] | None) -> list[str]:
        vec = embed_query(query)
        where_clauses: list[dict] = []
        if date_from:
            where_clauses.append({"published_ts": {"$gte": _published_ts(date_from)}})
        if date_to:
            where_clauses.append({"published_ts": {"$lte": _published_ts(date_to)}})
        if sub_buckets:
            where_clauses.append({"sub_bucket": {"$in": sub_buckets}})
        where = None
        if len(where_clauses) == 1:
            where = where_clauses[0]
        elif where_clauses:
            where = {"$and": where_clauses}
        ranked: list[tuple[float, str]] = []
        for bucket in buckets:  # bucket pre-filter = separate index per bucket
            col = collection(bucket, self._chroma)
            if col.count() == 0:
                continue
            res = col.query(query_embeddings=[vec], n_results=min(n, col.count()),
                            where=where, include=["distances"])
            for chunk_id, dist in zip(res["ids"][0], res["distances"][0]):
                ranked.append((dist, chunk_id))
        ranked.sort()
        return [cid for _, cid in ranked[:n]]

    def _keyword(self, query: str, buckets: list[str], n: int) -> list[str]:
        terms = [t for t in "".join(c if c.isalnum() else " " for c in query).split()
                 if len(t) > 1]
        if not terms:
            return []
        match = " OR ".join(f'"{t}"' for t in terms)
        ph = ",".join("?" for _ in buckets)
        try:
            rows = self.conn.execute(
                f"SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH ? "
                f"AND bucket IN ({ph}) ORDER BY bm25(chunks_fts) LIMIT ?",
                (match, *buckets, n),
            ).fetchall()
        except sqlite3.OperationalError:  # malformed FTS query from odd input
            return []
        return [r["chunk_id"] for r in rows]

    def _hydrate(self, chunk_id: str, score: float, legs: list[str]) -> SearchResult | None:
        row = self.conn.execute(
            "SELECT c.chunk_id, c.text, c.doc_id, d.title, d.bucket, d.sub_bucket, "
            "d.source, d.canonical_url, d.published_at, d.is_stale, d.stale_reason, "
            "d.doc_type, d.duplicate_of FROM chunks c JOIN documents d USING (doc_id) "
            "WHERE c.chunk_id = ?", (chunk_id,),
        ).fetchone()
        if row is None or row["duplicate_of"] is not None:
            return None
        return SearchResult(
            chunk_id=row["chunk_id"], doc_id=row["doc_id"], title=row["title"],
            text=row["text"], score=score, bucket=row["bucket"],
            sub_bucket=row["sub_bucket"], source=row["source"],
            canonical_url=row["canonical_url"], published_at=row["published_at"],
            is_stale=bool(row["is_stale"]), stale_reason=row["stale_reason"],
            doc_type=row["doc_type"], legs=legs,
        )

    def log(self, query: str, buckets: list[str] | None, resolution: str,
            n_results: int, detail: str = "") -> None:
        db.log_query(self.conn, query, buckets, resolution, n_results, detail)
