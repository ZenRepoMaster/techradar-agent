"""Live-source fallback for queries the KB can't answer confidently.

Design choice (argued in docs/DESIGN.md): instead of open web search with
unknown provenance, the fallback re-queries the *same curated sources* live
(ArXiv search API, Federal Register API) beyond the last crawl. That keeps
provenance controlled while restoring freshness — the main thing a pre-crawled
KB gives up. Every fallback result is tagged ``resolution="web"`` and the
query log records which path answered.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

import httpx

from ..rag.retrieval import SearchResult

_ATOM = {"a": "http://www.w3.org/2005/Atom"}
_WS = re.compile(r"\s+")


def live_search(query: str, buckets: list[str], k: int = 5) -> list[SearchResult]:
    results: list[SearchResult] = []
    try:
        if "research" in buckets:
            results.extend(_arxiv_live(query, k))
        if "regulatory" in buckets:
            results.extend(_federal_register_live(query, k))
    except httpx.HTTPError:
        pass  # fallback is best-effort; the agent reports the gap instead
    return results[: k * 2]


def _arxiv_live(query: str, k: int) -> list[SearchResult]:
    resp = httpx.get(
        "https://export.arxiv.org/api/query",
        params={"search_query": f"all:{query}", "max_results": k,
                "sortBy": "relevance"},
        timeout=30,
    )
    resp.raise_for_status()
    out = []
    for entry in ET.fromstring(resp.text).iterfind("a:entry", _ATOM):
        url = entry.findtext("a:id", "", _ATOM)
        title = _WS.sub(" ", entry.findtext("a:title", "", _ATOM)).strip()
        summary = _WS.sub(" ", entry.findtext("a:summary", "", _ATOM)).strip()
        published = (entry.findtext("a:published", "", _ATOM) or "")[:10]
        out.append(SearchResult(
            chunk_id=f"live:{url}", doc_id=f"live:arxiv:{url.rsplit('/', 1)[-1]}",
            title=title, text=f"{title}\n\n{summary}", score=0.0,
            bucket="research", sub_bucket="live", source="arxiv_live",
            canonical_url=url, published_at=published or None,
            is_stale=False, stale_reason=None, doc_type="paper", legs=["web"],
        ))
    return out


def _federal_register_live(query: str, k: int) -> list[SearchResult]:
    resp = httpx.get(
        "https://www.federalregister.gov/api/v1/documents.json",
        params={"conditions[term]": query, "per_page": k, "order": "relevance",
                "fields[]": ["document_number", "title", "abstract", "html_url",
                             "publication_date", "type"]},
        timeout=30,
    )
    resp.raise_for_status()
    out = []
    for item in resp.json().get("results", []):
        title = (item.get("title") or "").strip()
        out.append(SearchResult(
            chunk_id=f"live:{item['document_number']}",
            doc_id=f"live:fr:{item['document_number']}",
            title=title,
            text=f"{title}\n\n{(item.get('abstract') or '').strip()}",
            score=0.0, bucket="regulatory", sub_bucket="live",
            source="federal_register_live",
            canonical_url=item.get("html_url", ""),
            published_at=item.get("publication_date"),
            is_stale=False, stale_reason=None,
            doc_type=(item.get("type") or "notice").lower().replace(" ", "_"),
            legs=["web"],
        ))
    return out
