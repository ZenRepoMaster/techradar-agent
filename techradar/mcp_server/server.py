"""MCP server: kb_search / kb_get_document / kb_status.

Design notes:
* Startup must be < 2s, so the embedding model and Chroma client are loaded
  lazily on the first kb_search call, not at import time. kb_status and
  kb_get_document only touch SQLite and respond immediately.
* Every response is stamped with ``data_freshness`` — the most recent
  successful fetch across sources — so a client can see how current the
  corpus is without a second call.

Run directly (stdio transport):  python -m techradar.mcp_server.server
Auto-discovery: .claude/mcp.json at the repo root registers this server.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from mcp.server.fastmcp import FastMCP

from .. import db

mcp = FastMCP("techradar-kb")


@lru_cache(maxsize=1)
def _conn():
    return db.connect()


@lru_cache(maxsize=1)
def _retriever():
    from ..rag.retrieval import Retriever  # heavy import deferred past startup
    return Retriever(_conn())


def _freshness() -> dict[str, Any]:
    rows = _conn().execute(
        "SELECT source, last_success_at FROM source_state"
    ).fetchall()
    per_source = {r["source"]: r["last_success_at"] for r in rows}
    newest = max((v for v in per_source.values() if v), default=None)
    return {"last_successful_fetch": newest, "per_source": per_source}


@mcp.tool()
def kb_search(query: str, buckets: list[str] | None = None,
              sub_buckets: list[str] | None = None, k: int = 8,
              date_from: str | None = None, date_to: str | None = None,
              exclude_stale: bool = False) -> str:
    """Semantic + keyword hybrid search over the energy-systems/AI-infrastructure KB.

    Args:
        query: Natural-language search query.
        buckets: Optional scope — any of "research", "regulatory", "practitioner".
            Scoping is an index-level pre-filter (per-bucket vector indexes).
        sub_buckets: Optional finer scope (e.g. ArXiv category "eess.SY",
            agency slug "federal-energy-regulatory-commission", repo name).
        k: Number of results (default 8).
        date_from / date_to: ISO dates bounding document publication dates.
        exclude_stale: Drop documents flagged stale by the lifecycle pass
            (superseded standards, outdated orders). Default False — stale
            results are returned but carry an explicit staleness flag.
    """
    r = _retriever()
    results = r.search(query, buckets=buckets, k=k, include_stale=not exclude_stale,
                       date_from=date_from, date_to=date_to, sub_buckets=sub_buckets)
    r.log(query, buckets, "kb", len(results), detail="mcp:kb_search")
    return json.dumps({
        "query": query,
        "scoped_buckets": buckets or ["research", "regulatory", "practitioner"],
        "results": [{
            "doc_id": x.doc_id,
            "title": x.title,
            "snippet": x.text[:400],
            "score": round(x.score, 4),
            "bucket": x.bucket,
            "sub_bucket": x.sub_bucket,
            "source": x.source,
            "doc_type": x.doc_type,
            "published_at": x.published_at,
            "url": x.canonical_url,
            "is_stale": x.is_stale,
            "stale_reason": x.stale_reason,
            "matched_legs": x.legs,
        } for x in results],
        "data_freshness": _freshness(),
    }, indent=2)


@mcp.tool()
def kb_get_document(doc_id: str) -> str:
    """Fetch full metadata and stored content for a document by its stable ID.

    Args:
        doc_id: The knowledge-base document ID (e.g. "arxiv:ab12cd34ef56aa00")
            as returned by kb_search.
    """
    doc = db.get_document(_conn(), doc_id)
    if doc is None:
        return json.dumps({"error": f"no document with id {doc_id!r}"})
    if doc.get("full_text") and len(doc["full_text"]) > 40_000:
        doc["full_text"] = doc["full_text"][:40_000] + "\n...[truncated]"
    doc["data_freshness"] = _freshness()
    return json.dumps(doc, indent=2)


@mcp.tool()
def kb_status() -> str:
    """Corpus health: document counts by bucket/source, last fetch per source,
    staleness totals, and index freshness (chunks embedded vs total)."""
    status = db.kb_status(_conn())
    status["data_freshness"] = _freshness()
    return json.dumps(status, indent=2)


if __name__ == "__main__":
    mcp.run()
