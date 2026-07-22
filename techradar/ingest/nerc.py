"""NERC Reliability Standards connector.

NERC's site is a JS SPA, but its Optimizely (Episerver) Content Delivery API
serves the standards catalog as structured JSON — number, status, title,
purpose, docket, effective dates, and document links. We walk the content tree:

    StandardsIndexPage (id=13) -> StandardsCategoryPage (BAL, CIP, ...)
        -> StandardsDetailPage (BAL-001-2, ...)

Lifecycle: a standard whose status signals inactivity is flagged stale at
ingest ("source page signals deprecation"); version supersession within a
family (BAL-001-1 -> BAL-001-2) is resolved by the staleness pass.
"""

from __future__ import annotations

import re
from typing import Any, Iterator

from ..schema import Document, make_doc_id
from .base import BaseConnector, FetchError, log

CONTENT_API = "https://www.nerc.com/api/episerver/v3.0/content"
INDEX_PAGE_ID = 13
_DOCKET_RE = re.compile(r"Docket No\.?\s*([A-Z]{2}\d{2}-\d+(?:-\d+)?)")
_ACTIVE_STATUSES = ("mandatory subject to enforcement", "subject to future enforcement")


class Connector(BaseConnector):
    def fetch(self, window_start: str | None, window_end: str | None) -> Iterator[Document]:
        families = self._children(INDEX_PAGE_ID)
        yielded = 0
        for family in families:
            fam_name = family.get("name", "")
            fam_id = (family.get("contentLink") or {}).get("id")
            if not fam_id:
                continue
            for page in self._children(fam_id):
                if "StandardsDetailPage" not in (page.get("contentType") or []):
                    continue
                try:
                    yield self._to_document(page, fam_name)
                    yielded += 1
                except FetchError as exc:
                    log.warning("nerc: skipping %s: %s", page.get("name"), exc)
        log.info("nerc: yielded %d standards", yielded)

    def _children(self, content_id: int) -> list[dict[str, Any]]:
        resp = self.get_with_retry(f"{CONTENT_API}/{content_id}/children",
                                   params={"expand": "*"}, min_interval=0.5)
        return resp.json()

    def _to_document(self, page: dict[str, Any], family: str) -> Document:
        pm = page.get("pageModel") or {}
        number = (pm.get("name") or page.get("name") or "").strip()
        if not number:
            raise FetchError("standard has no number")
        status = (pm.get("status") or "").strip()
        purpose = _strip_html(pm.get("purposeHtml") or "")
        effective = _to_iso(pm.get("effectiveDateDisplay"))
        source_ids = {"nerc_number": number}
        m = _DOCKET_RE.search(pm.get("publicNotes") or "")
        if m:
            source_ids["docket"] = m.group(1)
        pdf = ((pm.get("standardDocument") or {}).get("url") or "").strip()
        if pdf:
            source_ids["pdf_url"] = f"https://www.nerc.com{pdf}"
        inactive = bool(status) and status.lower() not in _ACTIVE_STATUSES
        abstract_parts = [f"Status: {status}." if status else "",
                          f"Purpose: {purpose}" if purpose else "",
                          f"Effective: {effective}." if effective else ""]
        return Document(
            doc_id=make_doc_id("nerc", number),
            canonical_url=page.get("url") or f"https://www.nerc.com/standards/reliability-standards",
            source=self.cfg.name,
            doc_type=self.cfg.doc_type,
            bucket=self.cfg.bucket,
            sub_bucket=family,
            title=f"{number} — {(pm.get('title') or '').strip()}",
            abstract=" ".join(x for x in abstract_parts if x),
            storage_mode=self.cfg.storage_mode,
            published_at=effective or _to_iso(pm.get("boardAdoptedDateDisplay")),
            domain_tags=[family, "reliability_standard", status.lower().replace(" ", "_")]
            if status else [family, "reliability_standard"],
            source_ids=source_ids,
            version=_version_of(number),
            is_stale=inactive,
            stale_reason=f"source status: {status}" if inactive else None,
        )


def _version_of(number: str) -> str:
    """BAL-001-2 -> '2'; CIP-003-8 -> '8'; fall back to full number."""
    m = re.match(r"^[A-Z]+-\d+-(.+)$", number)
    return m.group(1) if m else number


def _strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()


def _to_iso(display: str | None) -> str | None:
    """'07/01/2016' -> '2016-07-01'."""
    if not display:
        return None
    m = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", display.strip())
    return f"{m.group(3)}-{m.group(1)}-{m.group(2)}" if m else None
