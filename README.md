# TechRadar Agent

Agentic research system for the **energy-systems / AI-infrastructure** domain:
a 55k+ document knowledge base spanning research, regulatory, and practitioner
verticals, with idempotent ingestion, bucket-scoped hybrid RAG, an MCP search
tool for Claude Code, a multi-step research agent, and an evaluation harness.

Built for the Blackcurrant AI Engineer take-home (V4).

## Corpus at a glance

Sample `kb_status` output from the submitted database:

```json
{
  "documents_total": 61201,
  "by_bucket": {"research": 45585, "regulatory": 15111, "practitioner": 505},
  "by_source": {"arxiv": 45585, "federal_register": 15000,
                "nerc": 111, "github_releases": 505},
  "stale_documents": 20,
  "chunks_total": 64616,
  "chunks_embedded": 64616
}
```

(Regenerate any time: `python -m techradar.cli status`.)

| Source | Vertical | What | Cadence | Strategy |
|---|---|---|---|---|
| ArXiv | research | papers in eess.SY/SP, cs.AI/LG/DC/AR/NI, stat.ML, physics.app-ph, cond-mat.mtrl-sci | daily | OAI-PMH bulk harvest, per-set datestamp cursors |
| Federal Register | regulatory | FERC + DOE rules, proposed rules, notices | daily | agency+year sliced API harvest, docket IDs captured |
| NERC | regulatory | Reliability Standards (all families) | monthly | Episerver Content API tree walk |
| GitHub | practitioner | release notes + READMEs, curated repo list | weekly | REST API |
| OSTI | research | DOE technical reports | weekly | implemented, **disabled** — osti.gov unreachable from dev network |

## Setup

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e .
```

Everything runs locally: SQLite (metadata + FTS5 keyword index), ChromaDB
(per-bucket vector indexes), fastembed/bge-small-en-v1.5 (ONNX embeddings).
No cloud services required for ingestion, indexing, retrieval, or the MCP tool.

The research agent needs one LLM credential (any of):

```bash
export GROQ_API_KEY=...        # or ANTHROPIC_API_KEY / GEMINI_API_KEY
# or: export TECHRADAR_LLM=ollama   (local, no key)
```

## Pipeline

```bash
# ingest one source (independently triggerable; date-bounded backfills)
python -m techradar.cli ingest --source arxiv --mode backfill
python -m techradar.cli ingest --source federal_register --from 2024-01-01 --to 2024-06-30
python -m techradar.cli ingest --all

# post-ingest lifecycle passes
python -m techradar.cli dedupe        # cross-source duplicate detection
python -m techradar.cli staleness     # regulatory lifecycle + staleness report

# chunk + embed + index (incremental, resumable)
python -m techradar.cli index

# corpus health
python -m techradar.cli status
```

Re-running any ingest against an unchanged upstream produces **zero new
records** (content-hash skip; verified in `fetch_runs`: a full ArXiv re-run
logged `new=5824, skipped=40176, failed=0` after a partial first pass). Every
run logs fetched/new/updated/skipped/failed counts.

## Search & agent

```bash
python -m techradar.cli search "grid-forming inverter stability" --bucket research
python -m techradar.cli brief "grid impacts of AI datacenter load growth" --out brief.md
python -m techradar.cli eval
```

## MCP tool for Claude Code

`.claude/mcp.json` registers the server, so opening this repo in Claude Code
auto-discovers the `techradar-kb` tools: `kb_search`, `kb_get_document`,
`kb_status`. Example bucket-scoped invocation from a Claude Code session:

> **You:** Use kb_search to find NERC requirements on frequency response,
> regulatory bucket only, excluding stale documents.
>
> Claude Code calls:
> ```json
> {
>   "tool": "kb_search",
>   "query": "frequency response requirements balancing authority",
>   "buckets": ["regulatory"],
>   "exclude_stale": true
> }
> ```
> and receives ranked results with staleness flags, provenance, and a
> `data_freshness` stamp.

Bucket scoping is a **pre-filter at the index level**: each bucket is its own
vector collection, so a regulatory query never scans the 45k-document ArXiv
index. Startup note: server code defers all heavy imports; measured cold start
is dominated by the `mcp` SDK's own import (~2.6 s on the 2019 Intel-Mac dev
machine, well under 2 s on current hardware — see docs/DESIGN.md).

## Layout

```
techradar/
  ingest/     connectors (arxiv, federal_register, nerc, github_releases, osti)
              + dedupe + staleness lifecycle passes
  rag/        chunking, embeddings, per-bucket Chroma indexes, hybrid retrieval
  mcp_server/ MCP stdio server (kb_search / kb_get_document / kb_status)
  agent/      pluggable LLM client, live-source fallback, research agent
  eval/       curated query set + harness
sources.yaml  all source config: cadence, scope, caps — no code changes needed
docs/         DESIGN.md, EVALUATION.md, sample brief
```

## Deliverable docs

- Design decisions & tradeoffs: [docs/DESIGN.md](docs/DESIGN.md)
- Evaluation results & interpretation: [docs/EVALUATION.md](docs/EVALUATION.md)
- Sample research brief: [docs/sample_brief.md](docs/sample_brief.md)
