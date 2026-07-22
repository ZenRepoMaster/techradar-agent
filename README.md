# TechRadar Agent

Agentic research system for the **energy-systems / AI-infrastructure** domain:
a 60k+ document knowledge base spanning research, regulatory, and practitioner
verticals, with idempotent ingestion, bucket-scoped hybrid RAG, an MCP search
tool for Claude Code, a multi-step research agent, and an evaluation harness.

Built for the Blackcurrant AI Engineer take-home (V4).

---

## Quickstart — test it in ~5 minutes

**Prerequisites:** Python 3.12, macOS or Linux. No cloud, no paid services.
(An LLM key is needed *only* for the research-agent step — search and everything
else needs none.)

### 1. Install

```bash
git clone https://github.com/ZenRepoMaster/techradar-agent.git
cd techradar-agent
python3.12 -m venv .venv
.venv/bin/pip install -e .
```

### 2. Get a corpus (pick one)

```bash
# Option A — download the prebuilt 61k-document corpus (recommended, ~1 min)
curl -L -o corpus.tar.gz \
  https://github.com/ZenRepoMaster/techradar-agent/releases/latest/download/techradar-corpus.tar.gz
tar xzf corpus.tar.gz          # extracts SQLite DB + Chroma index into ./data

# Option B — build a small real corpus from scratch (~3-5 min, ~1-2k docs)
./scripts/seed_demo.sh
```

### 3. Test it — five commands, expected output shown

```bash
# (1) CORPUS HEALTH — should print ~61,201 documents across 3 buckets
.venv/bin/python -m techradar.cli status
#   → {"documents_total": 61201, "by_bucket": {"research": 45585,
#      "regulatory": 15111, "practitioner": 505}, "stale_documents": 20, ...}

# (2) BUCKET-SCOPED SEARCH — regulatory shelf only; top hit is the right standard
.venv/bin/python -m techradar.cli search "frequency response requirements" --bucket regulatory
#   → 0.033  nerc:85ed...  (regulatory/BAL)  BAL-003-2 — Frequency Response and Frequency Bias Setting

# (3) STALENESS FLAGGING — outdated regs are marked, and can be excluded
.venv/bin/python -m techradar.cli search "CIP cyber security standard" --bucket regulatory
#   → some results tagged [STALE]; add --include-stale to keep them, default keeps+flags

# (4) RESEARCH AGENT — full multi-step briefing (needs an LLM key, see below)
.venv/bin/python -m techradar.cli brief "grid impacts of AI datacenter load growth" --out brief.md
#   → writes brief.md: key findings, regulatory-vs-research analysis, cited evidence, gaps

# (5) EVALUATION HARNESS — the graded metrics (retrieval, routing, staleness, citations)
.venv/bin/python -m techradar.cli eval
#   → prints hit_rate_at_k, mrr_at_k, routing p@5, stale-surfacing rate, citation accuracy
```

Also: `.venv/bin/python -m pytest tests/ -q` runs 13 network-free tests of the
pipeline invariants (idempotency, chunking, staleness, dedupe).

### 4. LLM key (only for step 4, the agent)

Put one key in a `.env` file at the repo root (a template is in `.env.example`):

```bash
cp .env.example .env
# then edit .env — set ONE of:
#   ANTHROPIC_API_KEY=sk-ant-...     (Anthropic free tier — the assessment's approved option)
#   GROQ_API_KEY=gsk-...             (console.groq.com, free)
#   GEMINI_API_KEY=AIza...           (aistudio.google.com, free)
# or run a local model with no key:  TECHRADAR_LLM=ollama
```

### 5. MCP tool in Claude Code (the headline integration)

`.claude/mcp.json` is already in the repo, so **opening this folder in Claude
Code auto-discovers the knowledge base as a tool** — no manual setup. Then just
ask in chat, e.g. *"Use kb_search for NERC frequency-response requirements,
regulatory bucket, exclude stale docs."* Claude Code calls `kb_search` and gets
ranked results with staleness flags, provenance, and a freshness stamp. The
server also runs standalone: `.venv/bin/python -m techradar.mcp_server.server`.

---

## Corpus at a glance

Sample `kb_status` output from the submitted (prebuilt) corpus:

```json
{
  "documents_total": 61201,
  "by_bucket": {"research": 45585, "regulatory": 15111, "practitioner": 505},
  "by_source": {"arxiv": 45585, "federal_register": 15000,
                "nerc": 111, "github_releases": 505},
  "stale_documents": 20,
  "cross_source_duplicates": 0,
  "chunks_total": 64709,
  "chunks_embedded": 64709
}
```

| Source | Vertical | What | Cadence | Fetch strategy |
|---|---|---|---|---|
| ArXiv | research | papers in eess.SY/SP, cs.AI/LG/DC/AR/NI, stat.ML, physics.app-ph, cond-mat.mtrl-sci | daily | OAI-PMH bulk harvest, per-set datestamp cursors |
| Federal Register | regulatory | FERC + DOE rules, proposed rules, notices | daily | agency+year sliced API harvest, docket IDs captured |
| NERC | regulatory | Reliability Standards (all families) | monthly | Episerver Content API tree walk |
| GitHub | practitioner | release notes + READMEs, curated repo list | weekly | REST API |
| OSTI | research | DOE technical reports | weekly | implemented, **disabled** — osti.gov was unreachable from the dev network |

---

## Full CLI reference

```bash
# Ingest — each source is independently triggerable; date-bounded backfills.
# All source behavior (cadence, scope, caps) lives in sources.yaml — no code edits.
python -m techradar.cli ingest --source arxiv --mode backfill
python -m techradar.cli ingest --source federal_register --from 2024-01-01 --to 2024-06-30
python -m techradar.cli ingest --all

# Lifecycle passes
python -m techradar.cli dedupe        # cross-source duplicate detection (DOI/ArXiv-ID, then fuzzy)
python -m techradar.cli staleness     # regulatory lifecycle + writes data/staleness_report.json

# Chunk + embed + index (incremental, resumable; --bucket to parallelize)
python -m techradar.cli index

# Query / agent / eval
python -m techradar.cli status
python -m techradar.cli search "grid-forming inverter stability" --bucket research --k 8
python -m techradar.cli brief "..." --out brief.md
python -m techradar.cli eval
```

Re-running any ingest against an unchanged upstream produces **zero new
records** (content-hash skip; verified — a full ArXiv re-run logged
`new=0, skipped=45585, failed=0`). Every run logs fetched/new/updated/
skipped/failed counts to the `fetch_runs` table.

**Bucket scoping is a pre-filter at the index level:** each bucket is its own
Chroma collection, so a regulatory query never scans the 45k-document ArXiv
index. MCP server cold start (spawn → import → initialize) is ~1.8 s, under the
2 s requirement — see [docs/DESIGN.md](docs/DESIGN.md).

---

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
tests/        13 network-free pytest cases
docs/         DESIGN.md, EVALUATION.md, sample_brief.md
scripts/      seed_demo.sh (fast small corpus)
```

## Deliverable docs

- Setup + testing: this file
- Design decisions & tradeoffs: [docs/DESIGN.md](docs/DESIGN.md)
- Evaluation results & interpretation: [docs/EVALUATION.md](docs/EVALUATION.md)
- Sample research brief (spans all 3 buckets): [docs/sample_brief.md](docs/sample_brief.md)
- MCP config: [.claude/mcp.json](.claude/mcp.json)
