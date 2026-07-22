"""Connector contract and the shared ingestion runner.

The runner owns idempotency accounting and logging so individual connectors
only implement ``fetch`` — yielding typed :class:`Document` objects.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from typing import Any, Iterator

import httpx

from ..config import Settings, SourceConfig
from ..db import (UpsertResult, connect, finish_run, get_cursor, set_cursor,
                  start_run, upsert_document)
from ..schema import Document

log = logging.getLogger("techradar.ingest")


class FetchError(Exception):
    """A document-level failure that should be counted, not fatal."""


class BaseConnector(ABC):
    def __init__(self, settings: Settings, cfg: SourceConfig, cursor: dict[str, Any]):
        self.settings = settings
        self.cfg = cfg
        self.cursor = cursor  # mutated in place; persisted by the runner on success
        self.client = httpx.Client(
            timeout=settings.request_timeout,
            headers={"User-Agent": settings.user_agent},
            follow_redirects=True,
        )

    @abstractmethod
    def fetch(self, window_start: str | None, window_end: str | None) -> Iterator[Document]:
        """Yield documents in the window. Raise FetchError per-document for soft failures."""

    def get_with_retry(self, url: str, params: dict[str, Any] | None = None,
                       min_interval: float = 0.0) -> httpx.Response:
        """GET with bounded retries; honors Retry-After on 429/503."""
        last: Exception | None = None
        for attempt in range(self.settings.retry_max):
            try:
                resp = self.client.get(url, params=params)
                if resp.status_code in (429, 503):
                    wait = float(resp.headers.get("Retry-After", self.settings.retry_backoff_seconds))
                    log.warning("%s -> %s, waiting %.0fs", url, resp.status_code, wait)
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                if min_interval:
                    time.sleep(min_interval)
                return resp
            except httpx.HTTPError as exc:
                last = exc
                wait = self.settings.retry_backoff_seconds * (attempt + 1)
                log.warning("%s failed (%s), retry in %.0fs", url, exc, wait)
                time.sleep(wait)
        raise FetchError(f"GET {url} failed after {self.settings.retry_max} attempts: {last}")

    def close(self) -> None:
        self.client.close()


def run_source(settings: Settings, source_name: str, mode: str = "on_demand",
               window_start: str | None = None, window_end: str | None = None) -> dict[str, int]:
    """Execute one ingestion run for a source and return the counts.

    Idempotency: re-running against an unchanged upstream yields zero new
    records (content-hash skip in ``upsert_document``). Every run is logged to
    ``fetch_runs`` with fetched/new/updated/skipped/failed counts.
    """
    from . import get_connector  # local import to avoid cycle

    cfg = settings.source(source_name)
    if not cfg.enabled:
        log.info("source %s is disabled; skipping", source_name)
        return {}

    conn = connect()
    run_id = start_run(conn, source_name, mode, window_start, window_end)
    counts = {"fetched": 0, "new": 0, "updated": 0, "skipped": 0, "failed": 0}
    connector = get_connector(cfg.connector)(settings, cfg, get_cursor(conn, source_name))
    log.info("run %d: source=%s mode=%s window=[%s, %s]",
             run_id, source_name, mode, window_start, window_end)
    try:
        for doc in connector.fetch(window_start, window_end):
            counts["fetched"] += 1
            try:
                result = upsert_document(conn, doc)
                counts[result] += 1
            except Exception:
                counts["failed"] += 1
                log.exception("upsert failed for %s", doc.doc_id)
            if counts["fetched"] % 1000 == 0:
                conn.commit()
                log.info("run %d progress: %s", run_id, counts)
        conn.commit()
        set_cursor(conn, source_name, connector.cursor, success=True)
        finish_run(conn, run_id, counts, status="ok")
        log.info("run %d finished: %s", run_id, counts)
    except Exception as exc:
        conn.commit()  # keep documents ingested before the failure
        set_cursor(conn, source_name, connector.cursor, success=False)
        finish_run(conn, run_id, counts, status="error", error=str(exc))
        log.exception("run %d aborted: %s", run_id, counts)
        raise
    finally:
        connector.close()
        conn.close()
    return counts
