"""Cross-source duplicate detection.

Two passes, cheapest first:

1. Exact identifier match — DOI, then ArXiv ID, shared across sources.
2. Fuzzy fallback — normalized-title blocking, then token-Jaccard on abstracts
   within each block. Blocking keeps this linear-ish at 50k+ docs; the O(n^2)
   comparison only happens inside tiny same-title groups.

The canonical copy is chosen by source priority (arxiv > osti > everything
else: the preprint server is the richer, versioned record for papers), then by
earliest publication. Non-canonical copies get ``duplicate_of`` set and are
excluded from canonical counts and retrieval ranking.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict

_SOURCE_PRIORITY = {"arxiv": 0, "osti": 1}
_NORM_RE = re.compile(r"[^a-z0-9 ]+")


def _norm_title(title: str) -> str:
    return _NORM_RE.sub("", title.lower()).strip()


def _tokens(text: str) -> set[str]:
    return set(_NORM_RE.sub(" ", text.lower()).split())


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _pick_canonical(rows: list[sqlite3.Row]) -> sqlite3.Row:
    return min(rows, key=lambda r: (_SOURCE_PRIORITY.get(r["source"], 9),
                                    r["published_at"] or "9999"))


def run_dedupe(conn: sqlite3.Connection) -> dict[str, int]:
    stats = {"id_groups": 0, "fuzzy_groups": 0, "marked": 0}
    rows = conn.execute(
        "SELECT doc_id, source, title, abstract, published_at, source_ids "
        "FROM documents WHERE duplicate_of IS NULL"
    ).fetchall()

    # pass 1: exact identifiers
    by_key: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        ids = json.loads(r["source_ids"])
        for key_name in ("doi", "arxiv_id"):
            if ids.get(key_name):
                by_key[f"{key_name}:{ids[key_name]}"].append(r)
    marked: set[str] = set()
    for group in by_key.values():
        distinct = [r for r in group if r["doc_id"] not in marked]
        if len({r["source"] for r in distinct}) < 2:
            continue
        canonical = _pick_canonical(distinct)
        stats["id_groups"] += 1
        for r in distinct:
            if r["doc_id"] != canonical["doc_id"]:
                conn.execute("UPDATE documents SET duplicate_of = ? WHERE doc_id = ?",
                             (canonical["doc_id"], r["doc_id"]))
                marked.add(r["doc_id"])

    # pass 2: title blocking + abstract similarity (cross-source only)
    by_title: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        if r["doc_id"] in marked:
            continue
        key = _norm_title(r["title"])
        if len(key) >= 20:  # short titles collide too easily
            by_title[key].append(r)
    for group in by_title.values():
        if len(group) < 2 or len({r["source"] for r in group}) < 2:
            continue
        canonical = _pick_canonical(group)
        can_tokens = _tokens(canonical["abstract"])
        hit = False
        for r in group:
            if r["doc_id"] == canonical["doc_id"]:
                continue
            sim = _jaccard(can_tokens, _tokens(r["abstract"]))
            if sim >= 0.5 or not r["abstract"] or not canonical["abstract"]:
                conn.execute("UPDATE documents SET duplicate_of = ? WHERE doc_id = ?",
                             (canonical["doc_id"], r["doc_id"]))
                marked.add(r["doc_id"])
                hit = True
        stats["fuzzy_groups"] += int(hit)

    stats["marked"] = len(marked)
    conn.commit()
    return stats
