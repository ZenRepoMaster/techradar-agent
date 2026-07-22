"""Regulatory lifecycle: staleness detection and reporting.

Rules applied on every pass (idempotent):

  A. Effective period ended — ``effective_until`` in the past.
  B. Version supersession — a newer version of the same NERC standard family
     exists (BAL-001-1 is stale once BAL-001-2 is ingested); the stale record
     gets ``superseded_by`` pointing at the newer document.
  C. Docket chains (Federal Register) — a newer final rule in the same docket
     root marks earlier rules/proposed rules in that chain stale. This is the
     FERC order-revision pattern (e.g. Order 841 -> 841-A on docket RM16-23).
  D. Source-signaled deprecation — set at ingest time (e.g. NERC status not in
     the active set); re-counted here for the report.

Every pass emits a staleness report (returned + written to
``data/staleness_report.json``). Silently surfacing outdated regulatory
guidance is a correctness failure, so retrieval surfaces these flags and the
agent must annotate stale citations.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from datetime import date

from ..config import DATA_DIR
from ..schema import utcnow

_NERC_RE = re.compile(r"^([A-Z]+-\d+)-(\d+)([a-z]?)$")
_DOCKET_ROOT_RE = re.compile(r"^([A-Z]{2}\d{2}-\d+)")


def _mark(conn: sqlite3.Connection, doc_id: str, reason: str,
          superseded_by: str | None = None) -> bool:
    cur = conn.execute(
        "UPDATE documents SET is_stale = 1, stale_reason = ?, "
        "superseded_by = COALESCE(?, superseded_by) "
        "WHERE doc_id = ? AND (is_stale = 0 OR stale_reason IS NULL)",
        (reason, superseded_by, doc_id),
    )
    return cur.rowcount > 0


def _rule_effective_period(conn: sqlite3.Connection) -> int:
    today = date.today().isoformat()
    rows = conn.execute(
        "SELECT doc_id, effective_until FROM documents "
        "WHERE is_stale = 0 AND effective_until IS NOT NULL AND effective_until < ?",
        (today,),
    ).fetchall()
    return sum(_mark(conn, r["doc_id"], f"effective period ended {r['effective_until']}")
               for r in rows)


def _rule_nerc_versions(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        "SELECT doc_id, source_ids FROM documents WHERE source = 'nerc'"
    ).fetchall()
    families: dict[str, list[tuple[int, str, str]]] = defaultdict(list)
    for r in rows:
        number = json.loads(r["source_ids"]).get("nerc_number", "")
        m = _NERC_RE.match(number)
        if m:
            families[m.group(1)].append((int(m.group(2)), number, r["doc_id"]))
    flagged = 0
    for family, versions in families.items():
        if len(versions) < 2:
            continue
        versions.sort()
        latest_num, latest_doc = versions[-1][1], versions[-1][2]
        for _, number, doc_id in versions[:-1]:
            flagged += _mark(conn, doc_id,
                             f"superseded by newer version {latest_num}", latest_doc)
    return flagged


def _rule_docket_chains(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        "SELECT doc_id, doc_type, published_at, source_ids FROM documents "
        "WHERE source = 'federal_register' AND doc_type IN ('rule', 'proposed_rule')"
    ).fetchall()
    chains: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        for docket in json.loads(r["source_ids"]).get("dockets", "").split(";"):
            m = _DOCKET_ROOT_RE.match(docket.strip())
            if m:
                chains[m.group(1)].append(r)
    flagged = 0
    for root, members in chains.items():
        finals = [r for r in members if r["doc_type"] == "rule" and r["published_at"]]
        if not finals:
            continue
        newest = max(finals, key=lambda r: r["published_at"])
        for r in members:
            if r["doc_id"] == newest["doc_id"] or not r["published_at"]:
                continue
            if r["published_at"] < newest["published_at"]:
                flagged += _mark(
                    conn, r["doc_id"],
                    f"newer final rule in docket {root} ({newest['published_at']})",
                    newest["doc_id"],
                )
    return flagged


def run_staleness(conn: sqlite3.Connection) -> dict:
    newly = {
        "effective_period_ended": _rule_effective_period(conn),
        "nerc_version_superseded": _rule_nerc_versions(conn),
        "fr_docket_chain": _rule_docket_chains(conn),
    }
    conn.commit()
    by_reason: dict[str, int] = {}
    for r in conn.execute(
        "SELECT stale_reason, COUNT(*) n FROM documents WHERE is_stale = 1 "
        "GROUP BY stale_reason ORDER BY n DESC LIMIT 50"
    ):
        by_reason[r["stale_reason"] or "unspecified"] = r["n"]
    by_source = {r["source"]: r["n"] for r in conn.execute(
        "SELECT source, COUNT(*) n FROM documents WHERE is_stale = 1 GROUP BY source")}
    sample = [dict(r) for r in conn.execute(
        "SELECT doc_id, title, stale_reason, superseded_by FROM documents "
        "WHERE is_stale = 1 ORDER BY last_checked_at DESC LIMIT 10")]
    report = {
        "generated_at": utcnow(),
        "newly_flagged": newly,
        "total_stale": sum(by_source.values()),
        "stale_by_source": by_source,
        "stale_by_reason_top": by_reason,
        "sample": sample,
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "staleness_report.json").write_text(json.dumps(report, indent=2))
    return report
