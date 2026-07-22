"""ArXiv connector — OAI-PMH bulk harvester.

Why OAI-PMH and not the search API: the harvesting interface returns 1,000
records per page with resumption tokens and is explicitly intended for bulk
metadata collection, which makes a 50k-document corpus tractable in minutes
instead of hours. Records are filtered client-side to the target categories.

Incremental behavior: the per-set cursor stores the last harvested datestamp;
subsequent runs pass it as ``from``, so re-runs against an unchanged upstream
fetch only records arXiv itself marks as updated.
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from typing import Iterator

from ..schema import Document, make_doc_id
from .base import BaseConnector, log

OAI_URL = "https://oaipmh.arxiv.org/oai"
NS = {
    "oai": "http://www.openarchives.org/OAI/2.0/",
    "arxiv": "http://arxiv.org/OAI/arXiv/",
}
_WS = re.compile(r"\s+")


def _clean(text: str | None) -> str:
    return _WS.sub(" ", text or "").strip()


class Connector(BaseConnector):
    def fetch(self, window_start: str | None, window_end: str | None) -> Iterator[Document]:
        p = self.cfg.params
        targets = set(p["target_categories"])
        max_docs = int(p.get("max_docs_per_run", 60000))
        per_set_caps: dict[str, int] = p.get("max_docs_per_set", {})
        interval = float(p.get("min_seconds_between_requests", 3.1))
        yielded = 0
        for oai_set in p["oai_sets"]:
            if yielded >= max_docs:
                break
            set_cap = int(per_set_caps.get(oai_set, max_docs))
            set_yielded = 0
            set_cursors = self.cursor.setdefault("set_from", {})
            frm = window_start or set_cursors.get(oai_set) \
                or p.get("default_from", {}).get(oai_set, "2024-01-01")
            latest_datestamp = frm
            params: dict[str, str] | None = {
                "verb": "ListRecords", "metadataPrefix": "arXiv",
                "set": oai_set, "from": frm,
                **({"until": window_end} if window_end else {}),
            }
            log.info("arxiv: harvesting set=%s from=%s cap=%d", oai_set, frm, set_cap)
            while params is not None and yielded < max_docs and set_yielded < set_cap:
                resp = self.get_with_retry(OAI_URL, params=params, min_interval=interval)
                root = ET.fromstring(resp.text)
                err = root.find("oai:error", NS)
                if err is not None:
                    if err.get("code") == "noRecordsMatch":
                        break
                    raise RuntimeError(f"OAI error [{err.get('code')}]: {err.text}")
                for rec in root.iterfind(".//oai:record", NS):
                    header = rec.find("oai:header", NS)
                    datestamp = header.findtext("oai:datestamp", "", NS)
                    latest_datestamp = max(latest_datestamp, datestamp)
                    meta = rec.find(".//arxiv:arXiv", NS)
                    if meta is None:  # deleted record
                        continue
                    categories = _clean(meta.findtext("arxiv:categories", "", NS)).split()
                    if not targets.intersection(categories):
                        continue
                    doc = self._to_document(meta, categories)
                    yielded += 1
                    set_yielded += 1
                    yield doc
                    if yielded >= max_docs or set_yielded >= set_cap:
                        break
                token = root.findtext(".//oai:resumptionToken", None, NS)
                params = {"verb": "ListRecords", "resumptionToken": token} if token else None
            # persist per-set progress so an aborted run resumes, not restarts
            set_cursors[oai_set] = latest_datestamp
        log.info("arxiv: yielded %d matching documents", yielded)

    def _to_document(self, meta: ET.Element, categories: list[str]) -> Document:
        arxiv_id = _clean(meta.findtext("arxiv:id", "", NS))
        doi = _clean(meta.findtext("arxiv:doi", "", NS))
        created = _clean(meta.findtext("arxiv:created", "", NS))
        updated = _clean(meta.findtext("arxiv:updated", "", NS))
        source_ids = {"arxiv_id": arxiv_id}
        if doi:
            source_ids["doi"] = doi.split()[0].lower()
        return Document(
            doc_id=make_doc_id("arxiv", arxiv_id),
            canonical_url=f"https://arxiv.org/abs/{arxiv_id}",
            source=self.cfg.name,
            doc_type=self.cfg.doc_type,
            bucket=self.cfg.bucket,
            sub_bucket=categories[0] if categories else "",
            title=_clean(meta.findtext("arxiv:title", "", NS)),
            abstract=_clean(meta.findtext("arxiv:abstract", "", NS)),
            storage_mode=self.cfg.storage_mode,
            published_at=created or None,
            domain_tags=categories,
            source_ids=source_ids,
            version=updated or "1",
        )
