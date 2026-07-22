#!/usr/bin/env bash
# Build a small but REAL, runnable knowledge base in a few minutes.
#
# Uses the same production connectors with bounded scope so an evaluator can go
# from `git clone` to a working multi-bucket KB + index without the hours-long
# full crawl. Produces ~1,000-2,000 documents across all three buckets.
#
# For the full 50k+ corpus: download the prebuilt artifact (see README) or run
#   python -m techradar.cli ingest --all
#
# Usage:  ./scripts/seed_demo.sh          (writes to ./data)
#         TECHRADAR_DATA_DIR=/tmp/kb ./scripts/seed_demo.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-.venv/bin/python}"
FROM="${SEED_FROM:-2026-06-01}"   # ~7 weeks of ArXiv/FR for a few hundred docs each

echo "==> regulatory: NERC reliability standards (full catalog, ~110 docs)"
$PY -m techradar.cli ingest --source nerc

echo "==> practitioner: GitHub releases + READMEs (curated repos, ~500 docs)"
$PY -m techradar.cli ingest --source github_releases

echo "==> research: ArXiv slice since $FROM (target categories only)"
$PY -m techradar.cli ingest --source arxiv --from "$FROM" --mode backfill

echo "==> regulatory: Federal Register slice since $FROM"
$PY -m techradar.cli ingest --source federal_register --from "$FROM" --mode backfill

echo "==> lifecycle passes: dedupe + staleness"
$PY -m techradar.cli dedupe
$PY -m techradar.cli staleness

echo "==> chunk + embed + index (downloads the embedding model on first run)"
$PY -m techradar.cli index

echo "==> corpus health"
$PY -m techradar.cli status

cat <<'EOF'

Seed complete. Try it:
  .venv/bin/python -m techradar.cli search "grid frequency response" --bucket regulatory
  .venv/bin/python -m techradar.cli brief "inverter-based frequency response requirements" --out brief.md
  # MCP server (Claude Code auto-discovers via .claude/mcp.json):
  .venv/bin/python -m techradar.mcp_server.server
EOF
