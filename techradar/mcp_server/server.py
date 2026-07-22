"""MCP server: kb_search / kb_get_document / kb_status.

Uses the low-level MCP ``Server`` API rather than ``FastMCP`` for one concrete
reason: the assessment requires cold start < 2s, and the low-level import path
is ~0.5s cheaper than FastMCP's (FastMCP pulls in extra machinery we don't
need for three tools). Combined with deferring the embedding model / Chroma
client to the first kb_search call, this brings measured startup under 2s.

* kb_status and kb_get_document touch only SQLite and respond immediately.
* Every response is stamped with ``data_freshness`` (most recent successful
  fetch across sources) so a client sees corpus currency without a 2nd call.

Run directly (stdio transport):  python -m techradar.mcp_server.server
Auto-discovery: .claude/mcp.json at the repo root registers this server.
"""

from __future__ import annotations

import asyncio
import json
from functools import lru_cache
from typing import Any

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from .. import db

server: Server = Server("techradar-kb")


@lru_cache(maxsize=1)
def _conn():
    return db.connect(read_only=False)


@lru_cache(maxsize=1)
def _retriever():
    from ..rag.retrieval import Retriever  # heavy import deferred past startup
    return Retriever(_conn())


def _freshness() -> dict[str, Any]:
    rows = _conn().execute("SELECT source, last_success_at FROM source_state").fetchall()
    per_source = {r["source"]: r["last_success_at"] for r in rows}
    newest = max((v for v in per_source.values() if v), default=None)
    return {"last_successful_fetch": newest, "per_source": per_source}


_BUCKET_ENUM = ["research", "regulatory", "practitioner"]

TOOLS = [
    types.Tool(
        name="kb_search",
        description=(
            "Semantic + keyword hybrid search over the energy-systems / "
            "AI-infrastructure knowledge base. Scope to KB buckets (research, "
            "regulatory, practitioner) or sub-buckets; filter by date; optionally "
            "exclude documents flagged stale by the regulatory-lifecycle pass. "
            "Bucket scoping is an index-level pre-filter. Every result carries "
            "provenance and a staleness flag; the response is stamped with data "
            "freshness."),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural-language query."},
                "buckets": {"type": "array", "items": {"type": "string", "enum": _BUCKET_ENUM},
                            "description": "Optional bucket scope (index-level pre-filter)."},
                "sub_buckets": {"type": "array", "items": {"type": "string"},
                                "description": "Optional finer scope: ArXiv category "
                                "(e.g. eess.SY), agency slug, or repo name."},
                "k": {"type": "integer", "default": 8, "description": "Number of results."},
                "date_from": {"type": "string", "description": "ISO date lower bound (publication)."},
                "date_to": {"type": "string", "description": "ISO date upper bound (publication)."},
                "exclude_stale": {"type": "boolean", "default": False,
                                  "description": "Drop documents flagged stale (superseded "
                                  "standards, outdated orders). Default false: stale docs "
                                  "are returned with an explicit flag."},
            },
            "required": ["query"],
        },
    ),
    types.Tool(
        name="kb_get_document",
        description="Fetch full metadata and stored content for a document by its stable "
                    "ID (e.g. 'arxiv:ab12cd34ef56aa00') as returned by kb_search.",
        inputSchema={
            "type": "object",
            "properties": {"doc_id": {"type": "string", "description": "Knowledge-base document ID."}},
            "required": ["doc_id"],
        },
    ),
    types.Tool(
        name="kb_status",
        description="Corpus health: document counts by bucket/source, last fetch per source, "
                    "staleness totals, cross-source duplicates, and index freshness "
                    "(chunks embedded vs total).",
        inputSchema={"type": "object", "properties": {}},
    ),
]


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    if name == "kb_search":
        payload = _kb_search(**arguments)
    elif name == "kb_get_document":
        payload = _kb_get_document(arguments["doc_id"])
    elif name == "kb_status":
        payload = _kb_status()
    else:
        payload = {"error": f"unknown tool {name!r}"}
    return [types.TextContent(type="text", text=json.dumps(payload, indent=2))]


def _kb_search(query: str, buckets: list[str] | None = None,
               sub_buckets: list[str] | None = None, k: int = 8,
               date_from: str | None = None, date_to: str | None = None,
               exclude_stale: bool = False) -> dict[str, Any]:
    r = _retriever()
    results = r.search(query, buckets=buckets, k=k, include_stale=not exclude_stale,
                       date_from=date_from, date_to=date_to, sub_buckets=sub_buckets)
    r.log(query, buckets, "kb", len(results), detail="mcp:kb_search")
    return {
        "query": query,
        "scoped_buckets": buckets or _BUCKET_ENUM,
        "results": [{
            "doc_id": x.doc_id, "title": x.title, "snippet": x.text[:400],
            "score": round(x.score, 4), "bucket": x.bucket, "sub_bucket": x.sub_bucket,
            "source": x.source, "doc_type": x.doc_type, "published_at": x.published_at,
            "url": x.canonical_url, "is_stale": x.is_stale, "stale_reason": x.stale_reason,
            "matched_legs": x.legs,
        } for x in results],
        "data_freshness": _freshness(),
    }


def _kb_get_document(doc_id: str) -> dict[str, Any]:
    doc = db.get_document(_conn(), doc_id)
    if doc is None:
        return {"error": f"no document with id {doc_id!r}"}
    if doc.get("full_text") and len(doc["full_text"]) > 40_000:
        doc["full_text"] = doc["full_text"][:40_000] + "\n...[truncated]"
    doc["data_freshness"] = _freshness()
    return doc


def _kb_status() -> dict[str, Any]:
    status = db.kb_status(_conn())
    status["data_freshness"] = _freshness()
    return status


async def _main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
