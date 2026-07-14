"""End-to-end tests for Phase 6A calculate, audit, and A1 ingestion paths."""
from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import fakeredis.aioredis
import httpx
import pytest
from fastapi.testclient import TestClient

from ekrs_rag.main import app
from ekrs_rag.storage.documents import DocumentRepo


_ADMIN_KEY = "test-admin-key-32chars-xxxxxxxxx"


@pytest.fixture
def e2e_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[TestClient]:
    """Boot the real app lifespan with isolated storage and local fakes."""
    from ekrs_rag import main as main_module
    from ekrs_rag.core.config import settings

    qdrant = MagicMock()
    qdrant.ensure_collection.return_value = None
    qdrant.get_ingestion_status.return_value = None
    qdrant.upsert_chunks.return_value = 1
    fake_redis = fakeredis.aioredis.FakeRedis()

    monkeypatch.setattr(settings, "ADMIN_KEY", _ADMIN_KEY)
    monkeypatch.setattr(settings, "PARSER_TOKEN", "")
    monkeypatch.setattr(settings, "AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    monkeypatch.setattr(settings, "DOCUMENTS_DB_PATH", str(tmp_path / "documents.db"))
    monkeypatch.setattr(settings, "TASK_DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setattr(settings, "DEBUG_LOG_PATH", str(tmp_path / "debug.log"))
    monkeypatch.setenv("PARSER_TOKEN", "")
    monkeypatch.setenv("METRICS_HOST", "127.0.0.1")
    monkeypatch.setenv("METRICS_PORT", "0")
    monkeypatch.setattr(main_module, "QdrantManager", MagicMock(return_value=qdrant))
    monkeypatch.setattr(
        main_module.aioredis,
        "from_url",
        MagicMock(return_value=fake_redis),
    )
    monkeypatch.setattr(main_module, "setup_logging", MagicMock())
    # Defensive: clear any leaked dependency_overrides from prior tests
    # (e.g., test_ingestion_phase4 sets get_task_repo override without teardown)
    app.dependency_overrides.clear()

    with TestClient(app) as client:
        yield client

    app.dependency_overrides.clear()
    app.state.document_repo.close()
    app.state.task_repo.close()


def _post_calculate(client: TestClient) -> httpx.Response:
    return client.post(
        "/v1/calculate",
        json={
            "constraints": [
                {
                    "parameter": "temperature",
                    "operator": "<=",
                    "value": 100,
                    "unit": "°C",
                    "priority": "NATIONAL",
                    "confidence": 0.95,
                    "source": {"block_id": "b1"},
                }
            ],
            "scope_path": "industry/x",
            "strict": True,
        },
        headers={"X-Admin-Key": _ADMIN_KEY},
    )


def test_calculate_does_not_require_qdrant(e2e_client: TestClient) -> None:
    """Direct calculation succeeds while the lifespan uses a local Qdrant fake."""
    response = _post_calculate(e2e_client)

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["branches"]


def test_calculate_audit_records_lineage_snapshot(
    e2e_client: TestClient,
    tmp_path: Path,
) -> None:
    """A successful direct calculation persists its lineage in the audit event."""
    response = _post_calculate(e2e_client)

    assert response.status_code == 200
    entries = [
        json.loads(line)
        for line in (tmp_path / "audit.log").read_text(encoding="utf-8").splitlines()
        if line.startswith("{")
    ]
    solved = [entry for entry in entries if entry["event"] == "constraint_solved"]
    assert solved
    assert solved[-1]["lineage_snapshot"] == response.json()["data"]["lineage_snapshot"]
    assert "conflict_details" in solved[-1]


def test_ingestion_notify_persists_document_metadata(
    e2e_client: TestClient,
    tmp_path: Path,
) -> None:
    """A1 parser metadata reaches the lifespan-attached real DocumentRepo."""
    doc_id = "phase6a-a1-doc"
    response = e2e_client.post(
        "/v1/ingestion/notify",
        json={
            "trace_id": "phase6a-a1-trace",
            "doc_hash": "phase6a-a1-hash",
            "version": 1,
            "output_path": str(tmp_path / "parser-output"),
            "metadata": {
                "doc_metadata": {
                    "doc_id": doc_id,
                    "type": "standard",
                    "scope_path": "industry/pressure-vessel",
                    "status": "active",
                }
            },
        },
    )

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    repo = e2e_client.app.state.document_repo
    assert isinstance(repo, DocumentRepo)
    document = repo.get(doc_id)
    assert document is not None
    assert document.doc_id == doc_id
    assert document.doc_type == "standard"
    assert document.scope_path == "industry/pressure-vessel"
    assert document.status == "active"
