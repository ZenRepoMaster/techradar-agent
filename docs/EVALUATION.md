# Evaluation

Four metrics, all implemented in `techradar/eval/harness.py` and run against the
full submitted corpus (61,201 documents, 64,709 chunks, 100% embedded). Raw
output is in `data/eval_results.json`; reproduce with `python -m techradar.cli
eval`. The query set (`techradar/eval/queries.yaml`) is 15 queries with
expectation predicates tied to *known* corpus contents (specific NERC standard
numbers, FERC topics, repo names) so a "hit" is a real relevance judgment, not a
similarity tautology.

Numbers first, then what they do and don't tell us.

| Metric | Result |
|---|---|
| Retrieval hit@8 | **1.00** (15/15) |
| Retrieval MRR@8 | **1.00** |
| Mean bucket-routing p@5 (unscoped) | **0.83** |
| Stale-flag surfacing rate | **1.00** (12/12) |
| Agent citation accuracy | **1.00** (13/13, 0 fabricated) |
| KB-vs-web resolution split | 29 KB / 0 web |

---

## 1. Retrieval hit@8 and MRR@8 — 1.00 / 1.00

Every one of the 15 queries returned its expected document at **rank 1**, under
bucket-scoped hybrid retrieval (dense + BM25, RRF-fused).

**What this tells us:** the retrieval stack works correctly and precisely on
clear-intent, in-domain queries. Bucket scoping, hybrid fusion, and the
embedding choice are sound; there are no plumbing bugs silently dropping
relevant documents.

**What it does *not* tell us — and why I don't oversell a perfect score:** this
is a deliberately *legible* query set. The queries use distinctive domain terms
("grid-forming inverter", "BAL-001", "vLLM"), and the expectation predicate is
"a top-8 result from the right source whose title contains one of these
phrases." A perfect score means the system clears that bar everywhere, not that
retrieval is solved. It says little about (a) ambiguous or underspecified
queries, (b) queries whose answer lives in a document with an unobvious title,
or (c) ranking quality *below* rank 1. A perfect hit@8 with MRR@8 also = 1.00
means I have no discrimination left in this metric — it's saturated. The honest
next step (see §Limitations) is 50–100 human-judged query–document pairs and
nDCG, which would actually have headroom to move.

## 2. Bucket-routing p@5 — 0.83, and the one that matters

This is the metric I learned the most from. Each scoped query is *re-run
unscoped*, and I measure what fraction of the top-5 comes from the bucket a
domain expert would have routed it to. It asks: **if a user doesn't specify a
bucket, does the right vertical still surface?**

Mean 0.83, but the per-query spread is the point:

| Query | route p@5 | Reading |
|---|---|---|
| 10 of 13 scoped queries | 1.0 | right bucket dominates unscoped |
| reg-bal-frequency | 0.6 | research papers on frequency control compete with the NERC standard |
| reg-storage-markets | 0.4 | battery-storage *research* competes with the FERC storage order |
| **prac-powerflow** | **0.0** | **total miss — worth dissecting** |

`prac-powerflow` ("open source python library for power flow network
simulation") returns, unscoped, **five ArXiv research papers** — including one
literally titled *"An Open Source Power System Simulator in Python"* — and
**zero** practitioner results in the top 5. Scoped to `practitioner`, the same
query correctly returns `pandapower`, `pypowsybl`, and their release notes at
ranks 1–3.

**The honest finding:** unscoped retrieval is biased toward the largest bucket.
Research holds 45,585 documents; practitioner holds 505 (90:1). For any query
whose language overlaps research vocabulary, the sheer density of research
vectors wins the global ANN race regardless of which vertical the user actually
wanted. This is not a ranking bug — it's a corpus-imbalance property, and it
would only get worse as the ArXiv bucket grows.

**Why this validates the design rather than indicting it:** it is the empirical
argument *for* bucket scoping being a first-class, index-level control instead
of an afterthought. The MCP tool and the research agent never issue unscoped
queries blindly — the agent's LLM decomposition routes each sub-question to an
explicit bucket, and the MCP `kb_search` exposes `buckets` as a primary
argument. The 0.0 is what the system looks like with that control *turned off*;
0.83 average is the residual imbalance it exists to correct. If I were shipping
this, I'd also add per-bucket score normalization or a small routing classifier
for the unscoped path — noted in Limitations.

## 3. Stale-flag surfacing — 1.00 (12/12)

For 12 randomly sampled stale documents, I query the index by each document's
own title and check two things: is the document retrieved, and does the returned
result carry its staleness flag? All 12 were retrieved and all 12 arrived
flagged with a reason.

**What this tells us:** the correctness guarantee the assessment calls out —
never silently surfacing outdated regulatory guidance — holds mechanically.
Because retrieval hydrates every hit from SQLite (the authoritative store) at
query time rather than trusting flags frozen into the vector index at embed
time, a document flagged stale *after* it was indexed is still surfaced with the
current flag. This is verified independently by a unit test
(`test_stale_surfacing`-style checks in `tests/test_core.py`) and end-to-end
here.

**Caveat:** this measures *surfacing* (is the flag present when the doc is
retrieved), not *staleness recall* (are all truly-stale docs flagged). The
latter depends on the lifecycle heuristics, which are honestly partial — only 20
documents are flagged corpus-wide (17 NERC version-supersessions, 3 FR docket
chains). The FR docket-chain rule under-flags because docket numbers are sparse
on Federal Register *notices*; I'd raise recall by parsing docket roots out of
abstract text (see DESIGN.md, fragility #2).

## 4. Agent citation accuracy — 1.00 (13/13, 0 fabricated)

The agent generated a brief; of the 13 citation IDs it emitted, all 13 resolved
to documents that were actually in its retrieved context. Zero fabrications.

**What this tells us:** on this run the LLM did not invent citation IDs. More
importantly, the *user-facing* guarantee is 100% by construction regardless of
LLM behavior — synthesis citations are mechanically verified against the
retrieved set, and any unresolvable ID is stripped and reported before the brief
is returned (`invalid_citations`). So this metric measures the LLM's *raw
fidelity*, and the mechanical verifier is the safety net beneath it.

**What it does *not* tell us:** n = 1 brief, 13 citations — a small sample, and
it measures citation *existence*, not citation *support* (whether the cited
document actually backs the specific claim). Faithfulness-to-source is the
harder, more valuable metric; measuring it well needs an LLM-judge or human
annotation pass I did not build. I'd rather report the narrow thing I actually
measured than dress it up as faithfulness.

## 5. KB-vs-web resolution split — 29 KB / 0 web

Across the 29 retrieval calls logged during the eval + agent run, **all** were
answered from the KB; the live-source fallback fired zero times.

**Honest reading:** I *cannot* report a meaningful KB-vs-web split from this run,
because the eval queries are all in-domain against a 61k corpus, so none fell
below the 3-result fallback threshold. This confirms the KB-first path dominates
for in-domain work (the intended behavior) but leaves the fallback path
exercised only by manual/targeted tests, not by this harness. A fair evaluation
of the fallback would need deliberately out-of-corpus or beyond-last-crawl
queries — a query set I'd add with more time.

---

## Limitations and what I'd measure next

Ranked by how much they'd change my confidence in the system:

1. **Saturated retrieval metric.** hit@8 = MRR@8 = 1.00 has no headroom. Replace
   the title-phrase predicate with 50–100 human relevance judgments and report
   nDCG@10 — the only way to see ranking quality differences that this metric
   hides.
2. **No faithfulness metric.** Citation *accuracy* (does the ID exist) is not
   citation *support* (does the source back the claim). An LLM-judge
   faithfulness score over a dozen briefs is the highest-value addition.
3. **Unscoped routing bias is real** (§2). Add per-bucket score normalization or
   a lightweight routing classifier for the unscoped path; re-measure routing
   p@5 with the fix.
4. **Staleness recall is unmeasured.** I measured surfacing, not recall. Build a
   small labeled set of known-superseded orders/standards and measure what
   fraction the lifecycle rules actually catch.
5. **Fallback path untested by the harness.** Add an out-of-domain query set to
   exercise and measure the KB→live-source split honestly.

## Bottom line

The pipeline and retrieval are solid and correct on clear queries, and the two
correctness guarantees that matter most for this domain — stale-flag surfacing
and citation verifiability — hold at 100% and are backed by mechanism, not luck.
The most informative result is not a headline pass rate but the routing metric's
`prac-powerflow` = 0.0: a concrete, reproducible demonstration that unscoped
retrieval is swamped by the largest bucket, which is exactly the failure that
first-class bucket scoping exists to prevent. The weakest part of this
evaluation is that my strongest-looking number (retrieval) is saturated on an
easy query set; the honest headline is "correct and well-instrumented, not yet
stress-tested."
