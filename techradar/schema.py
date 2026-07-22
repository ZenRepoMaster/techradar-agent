"""Typed document schema and SQLite DDL.

Every ingested record — regardless of source — conforms to :class:`Document`.
Chunk records denormalize the parent's bucket metadata so retrieval can be
scoped without joining back to the documents table.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_doc_id(source: str, source_key: str) -> str:
    """Stable document ID: deterministic on (source, source-native key)."""
    return f"{source}:{hashlib.sha1(source_key.encode()).hexdigest()[:16]}"


def content_hash(*parts: str | None) -> str:
    """sha256 over the canonical content fields; detects byte-identical re-fetches."""
    h = hashlib.sha256()
    for p in parts:
        h.update((p or "").encode())
        h.update(b"\x1f")
    return h.hexdigest()


@dataclass
class Document:
    """Canonical typed record for every document in the knowledge base."""

    doc_id: str                      # stable ID, e.g. "arxiv:ab12cd34..."
    canonical_url: str
    source: str                      # arxiv | osti | federal_register | nerc | github_releases
    doc_type: str                    # paper | report | order | rule | standard | release | repo
    bucket: str                      # research | regulatory | practitioner
    sub_bucket: str                  # e.g. arxiv primary category, agency slug, repo name
    title: str
    abstract: str = ""
    full_text: str | None = None
    storage_mode: str = "abstract_only"   # full_text | abstract_only | link_only
    published_at: str | None = None       # ISO date
    fetched_at: str = field(default_factory=utcnow)
    last_checked_at: str = field(default_factory=utcnow)
    domain_tags: list[str] = field(default_factory=list)
    source_ids: dict[str, str] = field(default_factory=dict)  # arxiv_id, doi, osti_id, docket, fr_doc_no...
    version: str = "1"
    superseded_by: str | None = None      # doc_id of the superseding document
    duplicate_of: str | None = None       # doc_id of the canonical copy (cross-source dedup)
    is_stale: bool = False
    stale_reason: str | None = None
    effective_until: str | None = None    # regulatory: end of effective period, if known

    @property
    def natural_key(self) -> str:
        return f"{self.canonical_url}#{self.version}"

    @property
    def hash(self) -> str:
        return content_hash(
            self.title, self.abstract, self.full_text, self.published_at,
            self.version, json.dumps(self.source_ids, sort_keys=True),
        )

    def to_row(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["domain_tags"] = json.dumps(self.domain_tags)
        d["source_ids"] = json.dumps(self.source_ids, sort_keys=True)
        d["is_stale"] = int(self.is_stale)
        d["natural_key"] = self.natural_key
        d["content_hash"] = self.hash
        return d


DDL = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id          TEXT PRIMARY KEY,
    canonical_url   TEXT NOT NULL,
    source          TEXT NOT NULL,
    doc_type        TEXT NOT NULL,
    bucket          TEXT NOT NULL CHECK (bucket IN ('research','regulatory','practitioner')),
    sub_bucket      TEXT NOT NULL DEFAULT '',
    title           TEXT NOT NULL,
    abstract        TEXT NOT NULL DEFAULT '',
    full_text       TEXT,
    storage_mode    TEXT NOT NULL CHECK (storage_mode IN ('full_text','abstract_only','link_only')),
    published_at    TEXT,
    fetched_at      TEXT NOT NULL,
    last_checked_at TEXT NOT NULL,
    domain_tags     TEXT NOT NULL DEFAULT '[]',
    source_ids      TEXT NOT NULL DEFAULT '{}',
    version         TEXT NOT NULL DEFAULT '1',
    superseded_by   TEXT,
    duplicate_of    TEXT,
    is_stale        INTEGER NOT NULL DEFAULT 0,
    stale_reason    TEXT,
    effective_until TEXT,
    natural_key     TEXT NOT NULL UNIQUE,
    content_hash    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_docs_bucket    ON documents(bucket, sub_bucket);
CREATE INDEX IF NOT EXISTS idx_docs_source    ON documents(source);
CREATE INDEX IF NOT EXISTS idx_docs_published ON documents(published_at);
CREATE INDEX IF NOT EXISTS idx_docs_stale     ON documents(is_stale);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id    TEXT PRIMARY KEY,       -- "<doc_id>#<seq>"
    doc_id      TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    text        TEXT NOT NULL,
    -- bucket metadata denormalized onto every chunk for scoped retrieval
    bucket      TEXT NOT NULL,
    sub_bucket  TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL,
    published_at TEXT,
    embedded_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc      ON chunks(doc_id);
CREATE INDEX IF NOT EXISTS idx_chunks_embedded ON chunks(embedded_at);

-- keyword leg of hybrid retrieval
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, chunk_id UNINDEXED, bucket UNINDEXED, content=''
);

CREATE TABLE IF NOT EXISTS fetch_runs (
    run_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL,
    mode         TEXT NOT NULL,          -- scheduled | on_demand | backfill
    window_start TEXT,
    window_end   TEXT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    fetched      INTEGER NOT NULL DEFAULT 0,
    new          INTEGER NOT NULL DEFAULT 0,
    updated      INTEGER NOT NULL DEFAULT 0,
    skipped      INTEGER NOT NULL DEFAULT 0,
    failed       INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'running',
    error        TEXT
);

CREATE TABLE IF NOT EXISTS source_state (
    source          TEXT PRIMARY KEY,
    last_fetch_at   TEXT,
    last_success_at TEXT,
    cursor          TEXT NOT NULL DEFAULT '{}'   -- connector-specific resume state (JSON)
);

CREATE TABLE IF NOT EXISTS query_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    at            TEXT NOT NULL,
    query         TEXT NOT NULL,
    buckets       TEXT,
    resolution    TEXT NOT NULL,   -- kb | web_fallback | kb+web
    n_results     INTEGER,
    detail        TEXT
);
"""
