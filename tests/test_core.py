"""Fast, network-free tests for the pipeline's core invariants."""

from __future__ import annotations

import json

import pytest

from techradar import db
from techradar.ingest.dedupe import run_dedupe
from techradar.ingest.staleness import run_staleness
from techradar.rag.chunking import chunk_document
from techradar.schema import Document, make_doc_id


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "test.db")
    yield c
    c.close()


def _doc(**over) -> Document:
    base = dict(
        doc_id=make_doc_id("arxiv", "2401.00001"),
        canonical_url="https://arxiv.org/abs/2401.00001",
        source="arxiv", doc_type="paper", bucket="research", sub_bucket="eess.SY",
        title="Grid-forming inverters", abstract="A study of inverter control.",
    )
    base.update(over)
    return Document(**base)


class TestIdempotency:
    def test_new_then_skip(self, conn):
        assert db.upsert_document(conn, _doc()) == "new"
        assert db.upsert_document(conn, _doc()) == "skipped"

    def test_content_change_updates_and_invalidates_chunks(self, conn):
        doc = _doc()
        db.upsert_document(conn, doc)
        conn.execute(
            "INSERT INTO chunks (chunk_id, doc_id, seq, text, bucket, sub_bucket, source) "
            "VALUES (?, ?, 0, 'x', 'research', 'eess.SY', 'arxiv')", (f"{doc.doc_id}#0", doc.doc_id))
        assert db.upsert_document(conn, _doc(abstract="Revised abstract v2.")) == "updated"
        n = conn.execute("SELECT COUNT(*) FROM chunks WHERE doc_id = ?",
                         (doc.doc_id,)).fetchone()[0]
        assert n == 0, "changed content must invalidate chunks for re-embedding"

    def test_update_preserves_original_fetched_at(self, conn):
        first = _doc(fetched_at="2026-01-01T00:00:00Z")
        db.upsert_document(conn, first)
        db.upsert_document(conn, _doc(abstract="changed"))
        row = conn.execute("SELECT fetched_at FROM documents WHERE doc_id = ?",
                           (first.doc_id,)).fetchone()
        assert row["fetched_at"] == "2026-01-01T00:00:00Z"


class TestChunking:
    def test_abstract_only_single_chunk(self):
        chunks = chunk_document({"title": "T", "abstract": "Short abstract.",
                                 "storage_mode": "abstract_only", "full_text": None})
        assert len(chunks) == 1 and chunks[0].startswith("T")

    def test_markdown_splits_on_headings(self):
        text = "# Intro\n" + "intro text. " * 50 + "\n# Usage\n" + "usage text. " * 200
        chunks = chunk_document({"title": "Repo", "abstract": "",
                                 "storage_mode": "full_text", "full_text": text})
        assert len(chunks) >= 2
        assert all(c.startswith("Repo") for c in chunks), "chunks carry title context"

    def test_oversize_sections_overlap(self):
        text = "word " * 2000  # single huge section, no headings
        chunks = chunk_document({"title": "T", "abstract": "",
                                 "storage_mode": "full_text", "full_text": text})
        assert len(chunks) > 1
        assert all(len(c) < 2200 for c in chunks)


class TestStaleness:
    def _nerc(self, number: str, doc_id_key: str | None = None) -> Document:
        return _doc(
            doc_id=make_doc_id("nerc", doc_id_key or number),
            canonical_url=f"https://nerc.com/{number}",
            source="nerc", doc_type="standard", bucket="regulatory",
            sub_bucket="BAL", title=number,
            source_ids={"nerc_number": number}, version=number.rsplit("-", 1)[-1],
        )

    def test_version_supersession(self, conn):
        db.upsert_document(conn, self._nerc("BAL-001-1"))
        db.upsert_document(conn, self._nerc("BAL-001-2"))
        report = run_staleness(conn)
        assert report["newly_flagged"]["nerc_version_superseded"] == 1
        old = conn.execute(
            "SELECT is_stale, superseded_by FROM documents WHERE doc_id = ?",
            (make_doc_id("nerc", "BAL-001-1"),)).fetchone()
        assert old["is_stale"] == 1
        assert old["superseded_by"] == make_doc_id("nerc", "BAL-001-2")

    def test_effective_period_ended(self, conn):
        db.upsert_document(conn, _doc(effective_until="2020-01-01"))
        report = run_staleness(conn)
        assert report["newly_flagged"]["effective_period_ended"] == 1

    def test_docket_chain(self, conn):
        proposed = _doc(
            doc_id=make_doc_id("federal_register", "P1"), source="federal_register",
            doc_type="proposed_rule", bucket="regulatory", canonical_url="u1",
            published_at="2018-01-01", source_ids={"dockets": "RM16-23-000"})
        final = _doc(
            doc_id=make_doc_id("federal_register", "F1"), source="federal_register",
            doc_type="rule", bucket="regulatory", canonical_url="u2",
            published_at="2020-06-01", source_ids={"dockets": "RM16-23-001"})
        db.upsert_document(conn, proposed)
        db.upsert_document(conn, final)
        report = run_staleness(conn)
        assert report["newly_flagged"]["fr_docket_chain"] == 1


class TestDedupe:
    def test_doi_exact_match_prefers_arxiv(self, conn):
        a = _doc(source_ids={"arxiv_id": "2401.00001", "doi": "10.1/x"})
        b = _doc(doc_id=make_doc_id("osti", "123"), canonical_url="https://osti.gov/123",
                 source="osti", source_ids={"osti_id": "123", "doi": "10.1/x"})
        db.upsert_document(conn, a)
        db.upsert_document(conn, b)
        stats = run_dedupe(conn)
        assert stats["marked"] == 1
        dup = conn.execute("SELECT duplicate_of FROM documents WHERE source='osti'").fetchone()
        assert dup["duplicate_of"] == a.doc_id

    def test_fuzzy_title_and_abstract(self, conn):
        a = _doc(title="Deep learning for power system state estimation methods",
                 abstract="We propose deep learning approaches for state estimation.")
        b = _doc(doc_id=make_doc_id("osti", "999"), canonical_url="https://osti.gov/999",
                 source="osti",
                 title="Deep Learning for Power System State Estimation Methods",
                 abstract="We propose deep learning approaches for state estimation!",
                 source_ids={"osti_id": "999"})
        db.upsert_document(conn, a)
        db.upsert_document(conn, b)
        assert run_dedupe(conn)["marked"] == 1

    def test_no_false_positive_same_source(self, conn):
        db.upsert_document(conn, _doc())
        db.upsert_document(conn, _doc(
            doc_id=make_doc_id("arxiv", "2401.00002"),
            canonical_url="https://arxiv.org/abs/2401.00002",
            source_ids={"arxiv_id": "2401.00002"}))
        assert run_dedupe(conn)["marked"] == 0


class TestStatus:
    def test_kb_status_shape(self, conn):
        db.upsert_document(conn, _doc())
        s = db.kb_status(conn)
        assert s["documents_total"] == 1
        assert s["by_bucket"] == {"research": 1}
        assert "generated_at" in s
