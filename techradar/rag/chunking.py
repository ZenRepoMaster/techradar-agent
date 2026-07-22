"""Chunking strategy — varies by storage mode / document shape.

* abstract-only records (papers, reports, regulatory metadata): title +
  abstract as a single chunk. Abstracts are 100-300 words, well inside the
  embedding model's useful range; splitting them only dilutes the signal.
* full-text records (READMEs, release notes): split on markdown headings
  first (they are natural topic boundaries), then re-split any oversized
  section at paragraph breaks. Target ~1400 chars (~350 tokens) with 200-char
  overlap between adjacent pieces of the same section.

Every chunk is prefixed with its document title so the embedding carries the
parent context even for deep sections.
"""

from __future__ import annotations

import re
from typing import Any

TARGET = 1400
OVERLAP = 200
_HEADING_RE = re.compile(r"^#{1,4}\s", re.MULTILINE)


def chunk_document(doc: dict[str, Any]) -> list[str]:
    title = doc["title"].strip()
    if doc["storage_mode"] == "full_text" and doc.get("full_text"):
        sections = _split_headings(doc["full_text"])
        pieces: list[str] = []
        for section in sections:
            pieces.extend(_split_size(section))
        return [f"{title}\n\n{p}" for p in pieces if p.strip()]
    # abstract_only / link_only: one chunk from title + abstract
    body = (doc.get("abstract") or "").strip()
    text = f"{title}\n\n{body}" if body else title
    return [text] if text.strip() else []


def _split_headings(text: str) -> list[str]:
    positions = [m.start() for m in _HEADING_RE.finditer(text)]
    if not positions:
        return [text]
    if positions[0] != 0:
        positions.insert(0, 0)
    sections = [text[a:b] for a, b in zip(positions, positions[1:] + [len(text)])]
    # merge tiny sections into their predecessor so we don't emit noise chunks
    merged: list[str] = []
    for s in sections:
        if merged and len(s) < 200:
            merged[-1] += s
        else:
            merged.append(s)
    return merged


def _split_size(text: str, target: int = TARGET, overlap: int = OVERLAP) -> list[str]:
    text = text.strip()
    if len(text) <= target:
        return [text]
    out: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + target, len(text))
        if end < len(text):
            # prefer to break at a paragraph, then a sentence, then hard cut
            for sep in ("\n\n", ". "):
                cut = text.rfind(sep, start + target // 2, end)
                if cut != -1:
                    end = cut + len(sep)
                    break
        out.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [p for p in out if p]
