"""Research agent: topic -> structured, citation-grounded research brief.

Reasoning loop (not a single retrieve-then-generate call):

  1. DECOMPOSE  — LLM splits the topic into sub-questions, each routed to the
                  KB bucket(s) most likely to answer it.
  2. RETRIEVE   — hybrid KB search per sub-question, bucket-scoped. If a
                  sub-question comes back thin (few hits / weak fusion score),
                  fall back to live source queries and log the resolution path.
  3. ITERATE    — LLM reviews the evidence, proposes follow-up queries for
                  gaps; one more retrieval round runs on those.
  4. SYNTHESIZE — LLM writes the brief citing [doc_id] markers only.
  5. VERIFY     — mechanical pass: every citation must resolve to a document
                  that was actually in the assembled context; stale citations
                  are re-flagged from the database, not trusted to the LLM.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from .. import db
from ..rag.retrieval import Retriever, SearchResult
from .llm import LLMClient
from .web_fallback import live_search

log = logging.getLogger("techradar.agent")

_CITE_RE = re.compile(r"\[((?:live:)?[a-z_]+:[A-Za-z0-9.#/\-]+)\]")
MIN_RESULTS = 3          # fewer KB hits than this triggers the live fallback
MAX_SUBQUESTIONS = 5
MAX_FOLLOWUPS = 2

_DECOMPOSE_SYSTEM = """\
You are the query planner for a research system over an energy-systems and
AI-infrastructure knowledge base with three buckets:
- research: ArXiv papers (power systems, grid control, ML, datacenters, HW)
- regulatory: FERC orders/rules, DOE regulatory documents, NERC reliability standards
- practitioner: release notes and READMEs of open-source energy/AI-infra tools

Decompose the topic into 2-{n} focused sub-questions. Route each to the
bucket(s) most likely to contain the answer. Output JSON:
[{{"question": "...", "buckets": ["research"], "why": "..."}}]"""

_GAP_SYSTEM = """\
You review evidence gathered for a research brief. Identify the most important
gaps — angles the evidence does not cover. Propose at most {n} follow-up search
queries (or an empty list if coverage is good). Output JSON:
[{{"question": "...", "buckets": ["regulatory"]}}]"""

_SYNTH_SYSTEM = """\
You write research briefs for domain experts in energy systems and AI
infrastructure. Ground every claim in the provided evidence and cite with the
bracketed document IDs exactly as given, e.g. [arxiv:ab12cd34ef567890]. Never
invent an ID. Evidence marked [STALE: ...] is outdated regulatory guidance —
if you use it, you must say it is outdated and why it still matters.
Where regulatory documents and academic literature align or conflict on the
same issue, say so explicitly.
Output JSON with keys:
  key_findings: [{"finding": str, "citations": [doc_id, ...]}]
  regulatory_vs_research: str   # alignment/conflict analysis, cited
  confidence_notes: str         # what is well-supported vs thin
  gaps: [str]
  followup_queries: [str]"""


@dataclass
class Brief:
    topic: str
    key_findings: list[dict]
    regulatory_vs_research: str
    confidence_notes: str
    gaps: list[str]
    followup_queries: list[str]
    citations: dict[str, dict]          # doc_id -> metadata for every cited doc
    resolution_paths: list[dict]        # per sub-question: kb | kb+web
    invalid_citations: list[str] = field(default_factory=list)
    provider: str = ""

    def to_markdown(self) -> str:
        lines = [f"# Research Brief: {self.topic}", ""]
        lines += ["## Key Findings", ""]
        for i, f in enumerate(self.key_findings, 1):
            cites = " ".join(f"[{c}]" for c in f.get("citations", []))
            lines.append(f"{i}. {f['finding']} {cites}")
        lines += ["", "## Regulatory vs. Research Alignment", "",
                  self.regulatory_vs_research,
                  "", "## Confidence Notes", "", self.confidence_notes,
                  "", "## Identified Gaps", ""]
        lines += [f"- {g}" for g in self.gaps] or ["- none identified"]
        lines += ["", "## Suggested Follow-up Queries", ""]
        lines += [f"- {q}" for q in self.followup_queries] or ["- none"]
        lines += ["", "## Evidence & Citations", ""]
        for doc_id, meta in sorted(self.citations.items()):
            stale = f" ⚠️ STALE — {meta['stale_reason']}" if meta.get("is_stale") else ""
            date = meta.get("published_at") or "n.d."
            lines.append(f"- **[{doc_id}]** {meta['title']} ({meta['bucket']}/"
                         f"{meta['source']}, {date}){stale}\n  {meta['url']}")
        if self.invalid_citations:
            lines += ["", f"> Synthesis referenced {len(self.invalid_citations)} "
                          f"unverifiable citation(s), removed: "
                          f"{', '.join(self.invalid_citations)}"]
        lines += ["", "---", f"*Resolution paths:*"]
        for rp in self.resolution_paths:
            lines.append(f"- \"{rp['question']}\" → {rp['resolution']} "
                         f"({rp['n_results']} results, buckets={rp['buckets']})")
        lines += [f"*LLM provider: {self.provider}*"]
        return "\n".join(lines)


class ResearchAgent:
    def __init__(self, llm: LLMClient | None = None, retriever: Retriever | None = None):
        self.llm = llm or LLMClient.from_env()
        self.retriever = retriever or Retriever()

    def run(self, topic: str, k_per_question: int = 6) -> Brief:
        subqs = self._decompose(topic)
        evidence: dict[str, SearchResult] = {}
        resolution_paths: list[dict] = []
        for sq in subqs:
            self._retrieve(sq, k_per_question, evidence, resolution_paths)

        followups = self._gap_check(topic, evidence)
        for sq in followups:
            self._retrieve(sq, k_per_question, evidence, resolution_paths,
                           phase="followup")

        return self._synthesize(topic, evidence, resolution_paths)

    # -- steps -------------------------------------------------------------

    def _decompose(self, topic: str) -> list[dict]:
        try:
            out = self.llm.complete_json(
                _DECOMPOSE_SYSTEM.format(n=MAX_SUBQUESTIONS),
                f"Topic: {topic}")
            subqs = [s for s in out if isinstance(s, dict) and s.get("question")]
        except Exception as exc:
            log.warning("decomposition failed (%s); using topic as single query", exc)
            subqs = []
        if not subqs:
            subqs = [{"question": topic, "buckets": None}]
        return subqs[:MAX_SUBQUESTIONS]

    def _retrieve(self, sq: dict, k: int, evidence: dict[str, SearchResult],
                  resolution_paths: list[dict], phase: str = "initial") -> None:
        buckets = sq.get("buckets") or None
        question = sq["question"]
        results = self.retriever.search(question, buckets=buckets, k=k)
        resolution = "kb"
        if len(results) < MIN_RESULTS:
            live = live_search(question, buckets or ["research", "regulatory"], k=4)
            if live:
                results.extend(live)
                resolution = "kb+web" if results else "web_fallback"
        for r in results:
            evidence.setdefault(r.doc_id, r)
        self.retriever.log(question, buckets, resolution, len(results),
                           detail=f"agent:{phase}")
        resolution_paths.append({"question": question, "buckets": buckets or "all",
                                 "resolution": resolution, "n_results": len(results)})

    def _gap_check(self, topic: str, evidence: dict[str, SearchResult]) -> list[dict]:
        if not evidence:
            return []
        summary = "\n".join(f"- [{r.doc_id}] {r.title}" for r in
                            list(evidence.values())[:40])
        try:
            out = self.llm.complete_json(
                _GAP_SYSTEM.format(n=MAX_FOLLOWUPS),
                f"Topic: {topic}\n\nEvidence collected so far:\n{summary}")
            return [s for s in out if isinstance(s, dict) and s.get("question")][:MAX_FOLLOWUPS]
        except Exception as exc:
            log.warning("gap check failed (%s); skipping follow-up round", exc)
            return []

    def _synthesize(self, topic: str, evidence: dict[str, SearchResult],
                    resolution_paths: list[dict]) -> Brief:
        blocks = [r.context_block() for r in evidence.values()]
        context = "\n\n---\n\n".join(blocks[:60])
        out = self.llm.complete_json(
            _SYNTH_SYSTEM,
            f"Topic: {topic}\n\nEvidence:\n\n{context}", max_tokens=4096)

        findings = out.get("key_findings", []) if isinstance(out, dict) else []
        # mechanical citation verification: LLM says [id]; database decides truth
        valid, invalid = {}, []
        def _check(ids: list[str]) -> list[str]:
            kept = []
            for doc_id in ids:
                r = evidence.get(doc_id)
                if r is None:
                    invalid.append(doc_id)
                    continue
                valid[doc_id] = {
                    "title": r.title, "bucket": r.bucket, "source": r.source,
                    "published_at": r.published_at, "url": r.canonical_url,
                    "is_stale": r.is_stale, "stale_reason": r.stale_reason,
                }
                kept.append(doc_id)
            return kept

        for f in findings:
            f["citations"] = _check([c.strip("[]") for c in f.get("citations", [])])
        for text_field in ("regulatory_vs_research", "confidence_notes"):
            for m in _CITE_RE.finditer(str(out.get(text_field, ""))):
                _check([m.group(1)])

        return Brief(
            topic=topic,
            key_findings=findings,
            regulatory_vs_research=str(out.get("regulatory_vs_research", "")),
            confidence_notes=str(out.get("confidence_notes", "")),
            gaps=[str(g) for g in out.get("gaps", [])],
            followup_queries=[str(q) for q in out.get("followup_queries", [])],
            citations=valid,
            resolution_paths=resolution_paths,
            invalid_citations=sorted(set(invalid)),
            provider=f"{self.llm.provider}:{self.llm.model}",
        )
