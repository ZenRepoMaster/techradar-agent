"""Federal Register connector — FERC orders/rules and DOE regulatory documents.

Uses the Federal Register's public JSON API. Each query is capped at 10,000
results server-side, so harvesting slices by (agency, year) windows and follows
``next_page_url`` cursors within each slice.

Staleness inputs captured here: document type, docket IDs (used by the
lifecycle pass to detect newer orders in the same docket chain), and
``effective_on`` dates.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Iterator

from ..schema import Document, make_doc_id
from .base import BaseConnector, log

API_URL = "https://www.federalregister.gov/api/v1/documents.json"
FIELDS = ["document_number", "title", "abstract", "type", "html_url",
          "publication_date", "effective_on", "docket_ids", "citation"]

_TYPE_MAP = {
    "Rule": "rule",
    "Proposed Rule": "proposed_rule",
    "Notice": "notice",
    "Presidential Document": "presidential_document",
}


class Connector(BaseConnector):
    def fetch(self, window_start: str | None, window_end: str | None) -> Iterator[Document]:
        p = self.cfg.params
        max_docs = int(p.get("max_docs_per_run", 15000))
        yielded = 0
        today = date.today().isoformat()
        for agency in p["agencies"]:
            if yielded >= max_docs:
                break
            agency_cursors = self.cursor.setdefault("agency_from", {})
            frm = window_start or agency_cursors.get(agency) or p.get("default_from", "2015-01-01")
            until = window_end or today
            # year slices keep every query under the API's 10k result cap
            for y0, y1 in _year_slices(frm, until):
                if yielded >= max_docs:
                    break
                for item in self._harvest_slice(agency, y0, y1):
                    yield self._to_document(item, agency)
                    yielded += 1
                    if yielded >= max_docs:
                        break
                agency_cursors[agency] = min(y1, until)
        log.info("federal_register: yielded %d documents", yielded)

    def _harvest_slice(self, agency: str, frm: str, until: str) -> Iterator[dict[str, Any]]:
        params: dict[str, Any] = {
            "conditions[agencies][]": agency,
            "conditions[publication_date][gte]": frm,
            "conditions[publication_date][lte]": until,
            "per_page": int(self.cfg.params.get("per_page", 100)),
            "order": "oldest",
            "fields[]": FIELDS,
        }
        url: str | None = API_URL
        while url:
            resp = self.get_with_retry(url, params=params, min_interval=0.6)
            data = resp.json()
            yield from data.get("results", [])
            url = data.get("next_page_url")
            params = None  # cursor URL already encodes the query

    def _to_document(self, item: dict[str, Any], agency: str) -> Document:
        doc_no = item["document_number"]
        source_ids = {"fr_doc_no": doc_no}
        if item.get("citation"):
            source_ids["citation"] = item["citation"]
        if item.get("docket_ids"):
            source_ids["dockets"] = ";".join(item["docket_ids"])
        return Document(
            doc_id=make_doc_id("federal_register", doc_no),
            canonical_url=item.get("html_url")
            or f"https://www.federalregister.gov/d/{doc_no}",
            source=self.cfg.name,
            doc_type=_TYPE_MAP.get(item.get("type", ""), "notice"),
            bucket=self.cfg.bucket,
            sub_bucket=agency,
            title=(item.get("title") or "").strip(),
            abstract=(item.get("abstract") or "").strip(),
            storage_mode=self.cfg.storage_mode,
            published_at=item.get("publication_date"),
            domain_tags=[agency, item.get("type", "").lower().replace(" ", "_")],
            source_ids=source_ids,
        )


def _year_slices(frm: str, until: str) -> list[tuple[str, str]]:
    """[(gte, lte)] pairs covering [frm, until] one calendar year at a time."""
    slices: list[tuple[str, str]] = []
    start_year, end_year = int(frm[:4]), int(until[:4])
    for year in range(start_year, end_year + 1):
        lo = frm if year == start_year else f"{year}-01-01"
        hi = until if year == end_year else f"{year}-12-31"
        slices.append((lo, hi))
    return slices
