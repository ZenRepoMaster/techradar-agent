"""GitHub connector — practitioner vertical.

Per curated repo: release notes (each release is a document, full text stored)
plus the repository README as a whitepaper-style overview document.

Auth: uses GITHUB_TOKEN if set, else falls back to `gh auth token` (the local
GitHub CLI), else unauthenticated (60 req/h — fine for the default repo list).
"""

from __future__ import annotations

import os
import subprocess
from typing import Any, Iterator

from ..schema import Document, make_doc_id
from .base import BaseConnector, log

API = "https://api.github.com"


def _token() -> str | None:
    if os.environ.get("GITHUB_TOKEN"):
        return os.environ["GITHUB_TOKEN"]
    try:
        out = subprocess.run(["gh", "auth", "token"], capture_output=True,
                             text=True, timeout=10)
        return out.stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


class Connector(BaseConnector):
    def fetch(self, window_start: str | None, window_end: str | None) -> Iterator[Document]:
        token = _token()
        if token:
            self.client.headers["Authorization"] = f"Bearer {token}"
        self.client.headers["Accept"] = "application/vnd.github+json"
        max_rel = int(self.cfg.params.get("max_releases_per_repo", 40))
        yielded = 0
        for repo in self.cfg.params["repos"]:
            try:
                readme = self._fetch_readme(repo)
                if readme:
                    yielded += 1
                    yield readme
                for rel in self._fetch_releases(repo, max_rel, window_start):
                    yielded += 1
                    yield rel
            except Exception as exc:  # one bad repo must not sink the run
                log.warning("github: %s failed: %s", repo, exc)
        log.info("github: yielded %d documents", yielded)

    def _fetch_readme(self, repo: str) -> Document | None:
        resp = self.client.get(f"{API}/repos/{repo}/readme")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        meta = resp.json()
        raw = self.client.get(meta["download_url"])
        raw.raise_for_status()
        text = raw.text[:200_000]
        return Document(
            doc_id=make_doc_id("github", f"{repo}#readme"),
            canonical_url=f"https://github.com/{repo}",
            source=self.cfg.name,
            doc_type="repo",
            bucket=self.cfg.bucket,
            sub_bucket=repo,
            title=f"{repo} — repository overview (README)",
            abstract=text[:600],
            full_text=text,
            storage_mode="full_text",
            domain_tags=[repo.split("/")[0], "readme"],
            source_ids={"github_repo": repo},
            version=meta.get("sha", "1"),  # README sha changes when content changes
        )

    def _fetch_releases(self, repo: str, cap: int,
                        window_start: str | None) -> Iterator[Document]:
        resp = self.client.get(f"{API}/repos/{repo}/releases",
                               params={"per_page": min(cap, 100)})
        if resp.status_code == 404:
            return
        resp.raise_for_status()
        for rel in resp.json()[:cap]:
            published = (rel.get("published_at") or "")[:10] or None
            if window_start and published and published < window_start:
                continue
            tag = rel.get("tag_name") or rel.get("name") or "untagged"
            body = (rel.get("body") or "").strip()
            yield Document(
                doc_id=make_doc_id("github", f"{repo}#release#{tag}"),
                canonical_url=rel.get("html_url") or f"https://github.com/{repo}/releases",
                source=self.cfg.name,
                doc_type=self.cfg.doc_type,
                bucket=self.cfg.bucket,
                sub_bucket=repo,
                title=f"{repo} {tag}" + (f" — {rel['name']}" if rel.get("name") else ""),
                abstract=body[:600],
                full_text=body or None,
                storage_mode="full_text" if body else "link_only",
                published_at=published,
                domain_tags=[repo.split("/")[0], "release_notes"],
                source_ids={"github_repo": repo, "release_tag": tag},
                version=tag,
            )
