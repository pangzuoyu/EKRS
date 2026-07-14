"""Tests for /v1/constraints/trace (spec §5, D8 prefix filter, A2 老数据 null)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ekrs_rag.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_trace_requires_parser_token_in_prod(monkeypatch, client):
    # PF1: settings loaded at import time → setattr is required (Pydantic cache).
    # Also setenv because current require_parser_token reads os.environ at call time.
    from ekrs_rag.core.config import settings as _settings
    monkeypatch.setattr(_settings, "PARSER_TOKEN", "test-parser-token-32chars-xxxxxxxx")
    monkeypatch.setenv("PARSER_TOKEN", "test-parser-token-32chars-xxxxxxxx")
    r = client.post("/v1/constraints/trace", json={"trace_id": "any"})
    assert r.status_code == 403  # existing require_parser_token returns 403


def test_trace_missing_trace_id_returns_422(client):
    r = client.post("/v1/constraints/trace", json={})
    assert r.status_code == 422


def test_trace_unknown_trace_id_returns_empty_events(client):
    r = client.post("/v1/constraints/trace", json={"trace_id": "no-such-trace-xyz"})
    assert r.status_code == 200
    body = r.json()
    assert body["trace_id"] == "no-such-trace-xyz"
    assert body["events"] == []
    assert body["lineage_snapshot"] is None
    assert body["conflict_details"] is None


def test_audit_index_seek_returns_only_matching_trace_id(tmp_path):
    """AuditIndex.seek returns events for one trace_id, not neighbors."""
    import json as _json
    from ekrs_rag.observability.audit_index import AuditIndex
    log = tmp_path / "audit.log"
    log.write_text("\n".join([
        _json.dumps({"event": "constraint_solve_started", "trace_id": "t1", "offset": 0, "raw": {"scope_path": "industry/petrochem"}}),
        _json.dumps({"event": "constraint_solve_started", "trace_id": "t2", "offset": 1, "raw": {"scope_path": "industry/power"}}),
    ]) + "\n")
    idx = AuditIndex(str(log))
    idx.build()
    got = idx.seek("t1")
    assert got is not None
    assert all(l.trace_id == "t1" for l in got)


def test_trace_returns_lineage_snapshot_from_constraint_solve_started_event(client, tmp_path, monkeypatch):
    """D5 + D2: lineage_snapshot pulled from constraint_solve_started event, not first event."""
    import json as _json
    log = tmp_path / "audit.log"
    # AuditIndex.seek() stores the entire JSON line as AuditLine.raw, so the
    # legacy fields are top-level on each line (not nested under "raw").
    log.write_text("\n".join([
        _json.dumps({"event": "endpoint_started", "trace_id": "sx", "offset": 0, "lineage_snapshot": "from_endpoint_BUG"}),
        _json.dumps({"event": "constraint_solve_started", "trace_id": "sx", "offset": 1, "lineage_snapshot": "from_solve_started_OK", "conflict_details": [{"type": "soft_fallback"}]}),
        _json.dumps({"event": "constraint_solved", "trace_id": "sx", "offset": 2, "lineage_snapshot": "from_solved_BUG"}),
    ]) + "\n")
    from ekrs_rag.observability.audit_index import AuditIndex
    idx = AuditIndex(str(log))
    idx.build()
    # Starlette State rejects monkeypatch.setattr (no __setattr__ for new keys),
    # so assign directly. State is shared across the TestClient by design.
    client.app.state.audit_index = idx
    r = client.post("/v1/constraints/trace", json={"trace_id": "sx"})
    body = r.json()
    assert body["lineage_snapshot"] == "from_solve_started_OK"
    assert body["conflict_details"] == [{"type": "soft_fallback"}]
