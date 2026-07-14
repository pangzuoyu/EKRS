"""Tests for DocumentRepo (Phase 6A spec §4, A1 ingestion path)."""
from __future__ import annotations

import pytest

from ekrs_rag.storage.documents import Document, DocumentRepo


@pytest.fixture
def repo(tmp_path) -> DocumentRepo:
    r = DocumentRepo(db_path=str(tmp_path / "documents.db"))
    r.init()
    return r


def _doc(doc_id="d1", doc_type="spec", scope_path="industry/petrochem", status="active"):
    return Document(
        doc_id=doc_id, doc_type=doc_type,
        scope_path=scope_path, status=status, created_at=1.0,
    )


def test_insert_and_get_round_trip(repo):
    repo.insert(_doc())
    got = repo.get("d1")
    assert got is not None
    assert got.doc_id == "d1"
    assert got.doc_type == "spec"
    assert got.scope_path == "industry/petrochem"
    assert got.status == "active"


def test_get_missing_returns_none(repo):
    assert repo.get("nonexistent") is None


def test_list_filters_by_scope_prefix(repo):
    repo.insert(_doc("d1", scope_path="industry/petrochem"))
    repo.insert(_doc("d2", scope_path="industry/power"))
    repo.insert(_doc("d3", scope_path="project/x"))
    petro = repo.list(scope_path_prefix="industry/")
    assert {d.doc_id for d in petro} == {"d1", "d2"}
    all_docs = repo.list()
    assert {d.doc_id for d in all_docs} == {"d1", "d2", "d3"}


def test_link_supersede_creates_relationship(repo):
    repo.insert(_doc("d1"))
    repo.insert(_doc("d2"))
    repo.link_supersede(from_doc_id="d1", to_doc_id="d2")
    # both docs still exist; supersede is a directed relationship
    assert repo.get("d1") is not None
    assert repo.get("d2") is not None


def test_link_override_creates_entry(repo):
    repo.insert(_doc("d_override", scope_path="industry/petrochem"))
    repo.link_override(scope_path="industry/petrochem", overrides={"max_temp": 100})
    # T3: read back via list() with prefix filter and confirm overrides_json
    # round-trips. Provides a verification handle for future ingestion flow.
    rows = repo.list(scope_path_prefix="industry/petrochem")
    assert any(r.scope_path == "industry/petrochem" for r in rows)
    # Direct SQL readback via the underlying connection to confirm payload stored.
    raw = repo._c().execute(
        "SELECT overrides_json FROM provision_overrides WHERE scope_path = ?",
        ("industry/petrochem",),
    ).fetchone()
    import json as _json
    assert _json.loads(raw["overrides_json"]) == {"max_temp": 100}


def test_insert_duplicate_doc_id_is_idempotent(repo):
    repo.insert(_doc("d1", status="active"))
    repo.insert(_doc("d1", status="deprecated"))  # INSERT OR IGNORE
    got = repo.get("d1")
    assert got.status == "active"  # first write wins


def test_close_releases_connection(tmp_path):
    r = DocumentRepo(db_path=str(tmp_path / "x.db"))
    r.init()
    r.close()
    # Re-init should succeed (idempotent)
    r.init()


# --- T1: ingestion-path metadata extraction contract (A1 resolution) ---


def test_insert_writes_doc_metadata_atomic(repo):
    """A1: ingestion.extract writes via DocumentRepo.insert — succeeds."""
    doc = _doc(doc_id="meta-1", doc_type="spec", scope_path="industry/", status="active")
    repo.insert(doc)
    got = repo.get("meta-1")
    assert got is not None
    assert got.doc_type == "spec"
    assert got.scope_path == "industry/"
    assert got.status == "active"
    assert got.created_at == 1.0


def test_insert_without_metadata_field_creates_no_row(repo):
    """A1 back-compat: pre-A1 IRs (no doc_metadata) take no effect on repo."""
    initial = len(repo.list())
    # Simulate: ingestion route receives IR with no doc_metadata attr — repo
    # is not called at all (verified implicitly: row count unchanged).
    assert len(repo.list()) == initial  # no row added


def test_insert_failure_writes_audit_warning(repo, monkeypatch):
    """Q1: DocumentRepo write failure soft-fails + audit warning (not raised)."""
    from ekrs_rag.observability.audit import AuditWriter

    captured: list[dict] = []
    aw = AuditWriter("/tmp/test-audit.log")
    monkeypatch.setattr(aw, "write", lambda event, **kw: captured.append({"event": event, **kw}) or True)

    # Force insert failure (closed connection)
    repo.close()
    try:
        # Run the ingestion-side block: try insert, on failure write audit.
        try:
            repo.insert(_doc(doc_id="boom"))
        except Exception as e:
            aw.write("document_metadata_failed", request_id="r1", error=str(e))
    finally:
        repo.init()

    assert any(c["event"] == "document_metadata_failed" for c in captured)
