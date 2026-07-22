# Design Document

## Decisions and why

**SQLite as the system of record, Chroma as a derived index.** Document
metadata, chunk records, fetch-run logs, cursors, and the query log live in
one SQLite file (WAL mode, 60s busy timeout — concurrent source backfills
interleave writes safely, which was validated the hard way, see Fragility).
The vector store is treated as a *rebuildable projection* of SQLite: retrieval
hydrates every hit from SQLite before returning it, so document-level facts
that change after indexing — staleness above all — are always authoritative.
The alternative (staleness flags in vector-store metadata) goes quietly wrong:
a standard superseded after embedding would keep its old flag until re-index.

**One vector collection per bucket = index-level pre-filtering.** A query
scoped to `regulatory` opens only the regulatory collection; the 45k-document
research index is never touched. Compared with metadata post-filtering this
(a) keeps ANN search on a ~15k index instead of ~65k chunks, and (b) removes
the classic post-filter failure where filtering top-k leaves you with 2 usable
results. Cost: unscoped queries fan out to three collections and fuse — an
acceptable price since scoped queries dominate the intended usage.

**Typed schema with three storage modes.** Full text is stored only where it
is small and high-value (READMEs, release notes, NERC purpose statements);
papers and Federal Register documents are abstract-only with a canonical URL;
anything else is link-only. Policy rationale: at 50k+ documents, abstracts
carry most of the retrieval signal per byte; full-text ArXiv PDFs would be
~100 GB, slow the crawl by two orders of magnitude, and mostly add chunks
that dilute ranking. The retrieval layer treats all three modes uniformly and
`kb_get_document` states the mode so consumers know what they got.

**Idempotency via two keys.** Stable `doc_id` = hash of (source, native ID);
change detection = content hash over canonical fields; `natural_key`
(URL + version) is a uniqueness backstop. Re-fetch of unchanged content is a
no-op that only bumps `last_checked_at`; changed content updates in place and
invalidates the document's chunks for automatic re-embedding.

**Chunking varies by document shape, deliberately.** Metadata records embed as
a single title+abstract chunk — splitting a 200-word abstract only dilutes
signal. Full-text markdown splits on headings first (topic boundaries), then
oversized sections at ~1400 chars with 200 overlap. Every chunk is prefixed
with its document title so deep sections keep parent context.

**bge-small-en-v1.5 via fastembed (ONNX).** Chosen over MiniLM-L6 (measurably
better retrieval at the same dimensionality/speed class) and over bge-base
(~3x slower on CPU; poor fit for local-only at this scale). A practical
constraint also applied: the dev machine is an Intel Mac where current torch
wheels don't exist, so sentence-transformers was out and ONNX runtime was the
reliable path. Asymmetric query prefixing follows the bge model card.

**Hybrid retrieval with RRF.** Dense-only retrieval is weak exactly where this
domain is precise: standard numbers ("BAL-001-2"), dockets ("RM22-14"),
version tags. FTS5/BM25 catches those; reciprocal-rank fusion combines the
legs without score-calibration headaches. Context assembly deduplicates to one
chunk per document and every result carries provenance + staleness flags.

## The storage / pull-on-demand tradeoff

A pre-crawled KB gives low latency, reproducible results, and controlled
provenance, but bounded coverage and freshness; live search inverts all of
that. This system is a hybrid — **KB first, live-source fallback** when a
sub-question returns fewer than 3 KB hits — with one deliberate narrowing:
the fallback re-queries the *same curated source APIs live* (ArXiv search,
Federal Register search) rather than the open web. Freshness beyond the last
crawl is restored while provenance stays known — every fallback result is
still a real paper or a real FR document with a canonical URL, not an SEO
page. The cost is bounded reach: a topic outside the curated sources fails
closed (reported as a gap in the brief) instead of failing open through
un-vetted web content. For a system whose failure mode of record is "surfaced
outdated or un-provenanced regulatory guidance", failing closed is the right
default. Every query logs its resolution path (`kb` / `kb+web`) to the query
log, and briefs print the paths per sub-question.

## Fragility — what I'd watch

1. **The NERC connector rides an undocumented API.** The standards catalog is
   served by NERC's Episerver Content Delivery API, discovered by inspecting
   their SPA (the HTML site is not scrapeable). It is structured and stable
   today, but it is not a published contract; a CMS migration breaks the
   connector silently. Mitigation: the run log makes a sudden `fetched: 0`
   visible, and the corpus keeps the last-good standards.
2. **FR docket-chain staleness is a heuristic.** "Newer final rule in the same
   docket root supersedes earlier documents" matches the FERC order-revision
   pattern (841 → 841-A) but will occasionally over-flag (a clarifying order
   that doesn't supersede) and under-flag (supersession across dockets).
   Flags are advisory annotations, not deletions, so the cost of an error is
   a wrong warning label, not lost content. Only 3 docs were flagged by this
   rule at submission — dockets are sparsely populated on FR notices, which
   limits recall; parsing docket numbers out of abstracts would improve it.
3. **Cross-source dedupe is idle in the submitted corpus.** With OSTI
   unreachable from the dev network (TCP timeouts — likely network policy),
   the only realistic DOI-overlap pair is missing, so `dedupe` correctly
   marks 0. The pass is implemented and unit-exercisable, but it has not been
   proven against real collisions — that is untested code in the honest sense.
4. **MCP startup budget is mostly the SDK.** The server uses the low-level
   `mcp.server.Server` API rather than `FastMCP` specifically to meet the 2 s
   startup requirement: FastMCP's import path costs ~0.5 s more than the
   low-level one for machinery three tools don't need. With that plus deferring
   the embedding model / Chroma client to the first `kb_search`, measured cold
   start (process spawn → import → MCP initialize handshake) is ~1.8 s on the
   2019 Intel i9 dev machine — under budget, but only ~0.1 s of it is this
   project's code; the rest is the SDK's own pydantic model compilation, so the
   margin is the SDK's to erode. Hand-rolling the JSON-RPC loop to dodge the
   SDK entirely would be protocol-fragile and was rejected deliberately.
5. **ArXiv taxonomy drift.** Category filters are config, not code, so adding
   a category is a YAML edit — but nobody is watching for *new* categories.

## What I'd do differently with more time

- **Reranking.** A cross-encoder rerank stage over the fused top-40 is the
  single highest-leverage retrieval improvement; skipped because CPU-only
  reranking at interactive latency needed more tuning time than it was worth
  against the eval deadline.
- **Real relevance judgments.** The eval predicates (source + title phrases)
  are honest but coarse; 50-100 human-judged query-document pairs would let
  me report true nDCG instead of hit@k proxies.
- **Incremental scheduling.** Cadences are declared in `sources.yaml` but
  nothing executes them; a small APScheduler loop (or cron calling the CLI)
  closes that gap.
- **Full-text for the regulatory bucket.** FR provides XML full text per
  document; abstract-only was the right scale tradeoff for this deadline, but
  regulatory full text is the one place full text changes answer quality
  (definitions and applicability sections carry the substance).
- **Prove the dedupe pass** against a live OSTI harvest from an unrestricted
  network.

## AI tooling note

This solution was built with AI coding assistance (Claude Code) driving
implementation under human direction, including live API-shape discovery
(probing the NERC/FR/OSTI endpoints before writing connectors), debugging
(the SQLite lock-contention incident recorded in `fetch_runs`), and the
evaluation design. All numbers reported in docs/EVALUATION.md come from
actual runs against the submitted corpus.
