"""DOE OSTI connector — E-Link records API.

NOTE: osti.gov was unreachable from the development network (connection
timeouts at the TCP level), so this connector is shipped implemented but
``enabled: false`` in sources.yaml. It targets the documented public records
endpoint and tolerates both v1 and v2 response envelopes; enable it and run
    python -m techradar.cli ingest --source osti --mode backfill
from a network that can reach osti.gov.
"""

from __future__ import annotations

from typing import Any, Iterator

from ..schema import Document, make_doc_id
from .base import BaseConnector, log

API_V1 = "https://www.osti.gov/api/v1/records"


class Connector(BaseConnector):
    def fetch(self, window_start: str | None, window_end: str | None) -> Iterator[Document]:
        p = self.cfg.params
        rows = int(p.get("rows_per_page", 100))
        max_docs = int(p.get("max_docs_per_run", 8000))
        yielded = 0
        for query in p["queries"]:
            page = 0
            while yielded < max_docs:
                params: dict[str, Any] = {"q": query, "rows": rows, "page": page}
                if window_start:
                    params["publication_date_start"] = window_start
                if window_end:
                    params["publication_date_end"] = window_end
                resp = self.get_with_retry(API_V1, params=params, min_interval=1.0)
                data = resp.json()
                records = data if isinstance(data, list) else data.get("data", [])
                if not records:
                    break
                for rec in records:
                    yield self._to_document(rec, query)
                    yielded += 1
                    if yielded >= max_docs:
                        break
                page += 1
        log.info("osti: yielded %d documents", yielded)

    def _to_document(self, rec: dict[str, Any], query: str) -> Document:
        osti_id = str(rec.get("osti_id") or rec.get("id"))
        source_ids = {"osti_id": osti_id}
        doi = (rec.get("doi") or "").strip()
        if doi:
            source_ids["doi"] = doi.lower().removeprefix("https://doi.org/")
        subjects = rec.get("subjects") or []
        if isinstance(subjects, str):
            subjects = [s.strip() for s in subjects.split(";") if s.strip()]
        return Document(
            doc_id=make_doc_id("osti", osti_id),
            canonical_url=f"https://www.osti.gov/biblio/{osti_id}",
            source=self.cfg.name,
            doc_type=self.cfg.doc_type,
            bucket=self.cfg.bucket,
            sub_bucket=(rec.get("product_type") or "technical_report").lower().replace(" ", "_"),
            title=(rec.get("title") or "").strip(),
            abstract=(rec.get("description") or rec.get("abstract") or "").strip(),
            storage_mode=self.cfg.storage_mode,
            published_at=(rec.get("publication_date") or "")[:10] or None,
            domain_tags=subjects[:8] + [f"query:{query}"],
            source_ids=source_ids,
        )
