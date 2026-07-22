"""Evaluation harness.

Implemented metrics (run, not just described):

1. retrieval hit@k        — does any top-k result satisfy the query's
                            expectation predicate (source + title phrases)?
2. bucket routing p@5     — for scoped queries re-run *unscoped*: what
                            fraction of the top-5 comes from the bucket a
                            domain expert would route to? Measures whether
                            cross-bucket ranking sends the right vertical to
                            the top when no scope is given.
3. stale surfacing        — for stale documents that retrieval returns, is
                            the staleness flag present on the result object?
                            This is the mechanical guarantee the brief's
                            freshness handling depends on; the harness
                            constructs queries directly from stale documents'
                            titles so the check actually exercises hits.
4. citation accuracy      — (requires an LLM key) generate a brief and
                            measure the fraction of citations that resolve to
                            documents that were really in the agent's context.
                            Skipped with a note when no provider is available.

Results land in data/eval_results.json; interpretation belongs in
docs/EVALUATION.md — numbers without commentary are not accepted output here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from ..config import DATA_DIR
from .. import db
from ..rag.retrieval import Retriever

QUERIES_FILE = Path(__file__).parent / "queries.yaml"


def _matches(result, expect: dict[str, Any]) -> bool:
    if expect.get("source") and result.source != expect["source"]:
        return False
    phrases = expect.get("title_contains_any") or []
    if phrases:
        title = result.title.lower()
        if not any(p.lower() in title for p in phrases):
            return False
    return True


def eval_retrieval(retriever: Retriever, spec: dict) -> dict[str, Any]:
    k = int(spec.get("k", 8))
    per_query = []
    for q in spec["queries"]:
        scoped = retriever.search(q["query"], buckets=q.get("scope"), k=k)
        hit_rank = next((i + 1 for i, r in enumerate(scoped)
                         if _matches(r, q["expect"])), None)
        row: dict[str, Any] = {
            "id": q["id"], "hit": hit_rank is not None, "hit_rank": hit_rank,
            "n_results": len(scoped),
            "top1": scoped[0].title[:90] if scoped else None,
        }
        # routing check: re-run unscoped, measure expected-bucket share of top-5
        if q.get("expect_bucket"):
            unscoped = retriever.search(q["query"], buckets=None, k=5)
            in_bucket = sum(r.bucket == q["expect_bucket"] for r in unscoped)
            row["routing_p_at_5"] = round(in_bucket / max(len(unscoped), 1), 3)
        per_query.append(row)

    hits = [r for r in per_query if r["hit"]]
    routing = [r["routing_p_at_5"] for r in per_query if "routing_p_at_5" in r]
    mrr = sum(1.0 / r["hit_rank"] for r in hits) / len(per_query)
    return {
        "k": k,
        "n_queries": len(per_query),
        "hit_rate_at_k": round(len(hits) / len(per_query), 3),
        "mrr_at_k": round(mrr, 3),
        "mean_routing_p_at_5": round(sum(routing) / len(routing), 3) if routing else None,
        "per_query": per_query,
    }


def eval_stale_surfacing(retriever: Retriever, sample: int = 12) -> dict[str, Any]:
    conn = retriever.conn
    stale_docs = conn.execute(
        "SELECT doc_id, title, bucket FROM documents WHERE is_stale = 1 "
        "AND duplicate_of IS NULL ORDER BY random() LIMIT ?", (sample,)
    ).fetchall()
    checked = flagged = surfaced = 0
    misses = []
    for doc in stale_docs:
        results = retriever.search(doc["title"][:120], buckets=[doc["bucket"]],
                                   k=8, include_stale=True)
        target = next((r for r in results if r.doc_id == doc["doc_id"]), None)
        if target is None:
            misses.append(doc["doc_id"])   # not retrieved by its own title
            continue
        surfaced += 1
        checked += 1
        flagged += int(target.is_stale and bool(target.stale_reason))
    return {
        "stale_docs_sampled": len(stale_docs),
        "retrieved_by_own_title": surfaced,
        "flag_present_when_surfaced": flagged,
        "flag_rate": round(flagged / checked, 3) if checked else None,
        "not_retrieved": misses,
    }


def eval_citations(topic: str = "grid impacts of AI data center load growth "
                                "and the regulatory response") -> dict[str, Any]:
    from ..agent.llm import LLMClient, LLMError
    from ..agent.research_agent import ResearchAgent
    try:
        llm = LLMClient.from_env()
        agent = ResearchAgent(llm=llm)
        brief = agent.run(topic)
    except LLMError as exc:
        return {"skipped": True, "reason": str(exc)}
    total = len(brief.citations) + len(brief.invalid_citations)
    return {
        "skipped": False,
        "provider": brief.provider,
        "topic": topic,
        "citations_valid": len(brief.citations),
        "citations_invalid": len(brief.invalid_citations),
        "citation_accuracy": round(len(brief.citations) / total, 3) if total else None,
        "stale_citations_flagged": sum(bool(m.get("is_stale"))
                                       for m in brief.citations.values()),
        "resolution_paths": brief.resolution_paths,
    }


def kb_web_split(conn) -> dict[str, int]:
    return {r["resolution"]: r["n"] for r in conn.execute(
        "SELECT resolution, COUNT(*) n FROM query_log GROUP BY resolution")}


def run_harness(with_agent: bool = True) -> dict[str, Any]:
    spec = yaml.safe_load(QUERIES_FILE.read_text())
    retriever = Retriever()
    results: dict[str, Any] = {
        "corpus": db.kb_status(retriever.conn),
        "retrieval": eval_retrieval(retriever, spec),
        "stale_surfacing": eval_stale_surfacing(retriever),
    }
    if with_agent:
        results["citations"] = eval_citations()
    results["kb_vs_web_resolution_split"] = kb_web_split(retriever.conn)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "eval_results.json").write_text(json.dumps(results, indent=2))
    return results
