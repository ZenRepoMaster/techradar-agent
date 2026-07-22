"""Command-line entry point.

    python -m techradar.cli ingest --source arxiv [--from D] [--to D] [--mode backfill]
    python -m techradar.cli ingest --all
    python -m techradar.cli status
    python -m techradar.cli dedupe
    python -m techradar.cli staleness
    python -m techradar.cli index [--batch 256]
    python -m techradar.cli search "query" [--bucket research] [--k 8]
    python -m techradar.cli brief "topic" [--out FILE]
    python -m techradar.cli eval
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from .config import load_settings
from . import db


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def cmd_ingest(args: argparse.Namespace) -> int:
    from .ingest.base import run_source

    settings = load_settings()
    names = list(settings.sources) if args.all else [args.source]
    if not names or names == [None]:
        print("error: pass --source NAME or --all", file=sys.stderr)
        return 2
    failures = 0
    for name in names:
        try:
            run_source(settings, name, mode=args.mode,
                       window_start=getattr(args, "from"), window_end=args.to)
        except Exception as exc:
            failures += 1
            print(f"source {name} failed: {exc}", file=sys.stderr)
    return 1 if failures else 0


def cmd_status(args: argparse.Namespace) -> int:
    conn = db.connect()
    print(json.dumps(db.kb_status(conn), indent=2))
    return 0


def cmd_dedupe(args: argparse.Namespace) -> int:
    from .ingest.dedupe import run_dedupe

    conn = db.connect()
    stats = run_dedupe(conn)
    print(json.dumps(stats, indent=2))
    return 0


def cmd_staleness(args: argparse.Namespace) -> int:
    from .ingest.staleness import run_staleness

    conn = db.connect()
    report = run_staleness(conn)
    print(json.dumps(report, indent=2))
    return 0


def cmd_index(args: argparse.Namespace) -> int:
    from .rag.index import build_index

    stats = build_index(batch_size=args.batch, limit=args.limit,
                        only_bucket=args.bucket)
    print(json.dumps(stats, indent=2))
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    from .rag.retrieval import Retriever

    retriever = Retriever()
    results = retriever.search(args.query, buckets=args.bucket or None, k=args.k,
                               include_stale=args.include_stale)
    for r in results:
        flag = " [STALE]" if r.is_stale else ""
        print(f"{r.score:.3f}  {r.doc_id}  ({r.bucket}/{r.sub_bucket}){flag}")
        print(f"       {r.title}")
    return 0


def cmd_brief(args: argparse.Namespace) -> int:
    from .agent.research_agent import ResearchAgent

    agent = ResearchAgent()
    brief = agent.run(args.topic)
    text = brief.to_markdown()
    if args.out:
        with open(args.out, "w") as f:
            f.write(text)
        print(f"brief written to {args.out}")
    else:
        print(text)
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    from .eval.harness import run_harness

    results = run_harness()
    print(json.dumps(results, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="techradar")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ingest", help="run one or all source connectors")
    p.add_argument("--source")
    p.add_argument("--all", action="store_true")
    p.add_argument("--from", dest="from", default=None, metavar="YYYY-MM-DD")
    p.add_argument("--to", default=None, metavar="YYYY-MM-DD")
    p.add_argument("--mode", default="on_demand",
                   choices=["on_demand", "scheduled", "backfill"])
    p.set_defaults(fn=cmd_ingest)

    p = sub.add_parser("status", help="corpus health statistics (kb_status)")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("dedupe", help="cross-source duplicate detection")
    p.set_defaults(fn=cmd_dedupe)

    p = sub.add_parser("staleness", help="regulatory lifecycle check + staleness report")
    p.set_defaults(fn=cmd_staleness)

    p = sub.add_parser("index", help="chunk + embed + index new documents")
    p.add_argument("--batch", type=int, default=256)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--bucket", choices=["research", "regulatory", "practitioner"],
                   help="restrict this run to one bucket (parallel embedding)")
    p.set_defaults(fn=cmd_index)

    p = sub.add_parser("search", help="hybrid KB search")
    p.add_argument("query")
    p.add_argument("--bucket", action="append", choices=["research", "regulatory", "practitioner"])
    p.add_argument("--k", type=int, default=8)
    p.add_argument("--include-stale", action="store_true")
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("brief", help="generate a research brief")
    p.add_argument("topic")
    p.add_argument("--out")
    p.set_defaults(fn=cmd_brief)

    p = sub.add_parser("eval", help="run the evaluation harness")
    p.set_defaults(fn=cmd_eval)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
