"""SQLite access layer.

Single-writer usage pattern (pipeline runs are sequential); WAL mode keeps the
MCP server's reads non-blocking while a crawl is in progress.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import DATA_DIR, DB_PATH
from .schema import DDL, Document, utcnow

_DOC_COLUMNS = [
    "doc_id", "canonical_url", "source", "doc_type", "bucket", "sub_bucket",
    "title", "abstract", "full_text", "storage_mode", "published_at",
    "fetched_at", "last_checked_at", "domain_tags", "source_ids", "version",
    "superseded_by", "duplicate_of", "is_stale", "stale_reason",
    "effective_until", "natural_key", "content_hash",
]


def connect(path: Path | None = None, read_only: bool = False) -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    target = path or DB_PATH
    if read_only:
        conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(target, timeout=60.0)
        conn.executescript(DDL)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=60000")  # concurrent source runs interleave writes
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


class UpsertResult:
    NEW = "new"
    UPDATED = "updated"
    SKIPPED = "skipped"


def upsert_document(conn: sqlite3.Connection, doc: Document) -> str:
    """Idempotent write: unchanged content -> SKIPPED, changed -> UPDATED.

    Identity is the stable ``doc_id``; change detection is the content hash.
    A changed ``natural_key`` (URL+version) with the same doc_id is an update
    (new version of the same underlying document).
    """
    row = doc.to_row()
    existing = conn.execute(
        "SELECT content_hash, fetched_at FROM documents WHERE doc_id = ?", (doc.doc_id,)
    ).fetchone()
    if existing is None:
        cols = ", ".join(_DOC_COLUMNS)
        ph = ", ".join(f":{c}" for c in _DOC_COLUMNS)
        conn.execute(f"INSERT INTO documents ({cols}) VALUES ({ph})", row)
        return UpsertResult.NEW
    if existing["content_hash"] == row["content_hash"]:
        conn.execute(
            "UPDATE documents SET last_checked_at = ? WHERE doc_id = ?",
            (utcnow(), doc.doc_id),
        )
        return UpsertResult.SKIPPED
    # content changed upstream: keep original fetched_at, refresh the rest
    row["fetched_at"] = existing["fetched_at"]
    assignments = ", ".join(f"{c} = :{c}" for c in _DOC_COLUMNS if c != "doc_id")
    conn.execute(f"UPDATE documents SET {assignments} WHERE doc_id = :doc_id", row)
    # content changed -> chunks are invalid; they will be re-chunked/re-embedded
    conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc.doc_id,))
    return UpsertResult.UPDATED


def get_document(conn: sqlite3.Connection, doc_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["domain_tags"] = json.loads(d["domain_tags"])
    d["source_ids"] = json.loads(d["source_ids"])
    d["is_stale"] = bool(d["is_stale"])
    return d


def start_run(conn: sqlite3.Connection, source: str, mode: str,
              window_start: str | None, window_end: str | None) -> int:
    cur = conn.execute(
        "INSERT INTO fetch_runs (source, mode, window_start, window_end, started_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (source, mode, window_start, window_end, utcnow()),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, counts: dict[str, int],
               status: str = "ok", error: str | None = None) -> None:
    conn.execute(
        "UPDATE fetch_runs SET finished_at=?, fetched=?, new=?, updated=?, skipped=?, "
        "failed=?, status=?, error=? WHERE run_id=?",
        (utcnow(), counts.get("fetched", 0), counts.get("new", 0),
         counts.get("updated", 0), counts.get("skipped", 0), counts.get("failed", 0),
         status, error, run_id),
    )
    conn.commit()


def get_cursor(conn: sqlite3.Connection, source: str) -> dict[str, Any]:
    row = conn.execute("SELECT cursor FROM source_state WHERE source = ?", (source,)).fetchone()
    return json.loads(row["cursor"]) if row else {}


def set_cursor(conn: sqlite3.Connection, source: str, cursor: dict[str, Any],
               success: bool = True) -> None:
    now = utcnow()
    conn.execute(
        "INSERT INTO source_state (source, last_fetch_at, last_success_at, cursor) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(source) DO UPDATE SET last_fetch_at = excluded.last_fetch_at, "
        "cursor = excluded.cursor" + (", last_success_at = excluded.last_success_at" if success else ""),
        (source, now, now if success else None, json.dumps(cursor)),
    )
    conn.commit()


def log_query(conn: sqlite3.Connection, query: str, buckets: list[str] | None,
              resolution: str, n_results: int, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO query_log (at, query, buckets, resolution, n_results, detail) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (utcnow(), query, json.dumps(buckets or []), resolution, n_results, detail),
    )
    conn.commit()


def kb_status(conn: sqlite3.Connection) -> dict[str, Any]:
    """Corpus health: counts by bucket/source, freshness, staleness, embedding progress."""
    by_bucket = {r["bucket"]: r["n"] for r in conn.execute(
        "SELECT bucket, COUNT(*) n FROM documents WHERE duplicate_of IS NULL GROUP BY bucket")}
    by_source = {r["source"]: r["n"] for r in conn.execute(
        "SELECT source, COUNT(*) n FROM documents GROUP BY source")}
    last_fetch = {r["source"]: r["last_success_at"] for r in conn.execute(
        "SELECT source, last_success_at FROM source_state")}
    stale = conn.execute("SELECT COUNT(*) n FROM documents WHERE is_stale = 1").fetchone()["n"]
    dupes = conn.execute(
        "SELECT COUNT(*) n FROM documents WHERE duplicate_of IS NOT NULL").fetchone()["n"]
    chunks_total = conn.execute("SELECT COUNT(*) n FROM chunks").fetchone()["n"]
    chunks_embedded = conn.execute(
        "SELECT COUNT(*) n FROM chunks WHERE embedded_at IS NOT NULL").fetchone()["n"]
    return {
        "documents_total": sum(by_source.values()),
        "documents_canonical": sum(by_bucket.values()),
        "by_bucket": by_bucket,
        "by_source": by_source,
        "last_success_per_source": last_fetch,
        "stale_documents": stale,
        "cross_source_duplicates": dupes,
        "chunks_total": chunks_total,
        "chunks_embedded": chunks_embedded,
        "generated_at": utcnow(),
    }
