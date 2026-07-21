"""Unit tests for IngestionOutcome frozen dataclass."""
from dataclasses import FrozenInstanceError

import pytest

from ekrs_rag.ingestion.outcome import IngestionOutcome


def test_outcome_success_default_chunks_zero():
    o = IngestionOutcome(rag_status="success")
    assert o.rag_status == "success"
    assert o.error is None
    assert o.error_code is None
    assert o.chunks_indexed == 0


def test_outcome_failed_with_error_and_code():
    o = IngestionOutcome(
        rag_status="failed",
        error="JSONL not found",
        error_code="jsonl_missing",
    )
    assert o.rag_status == "failed"
    assert o.error == "JSONL not found"
    assert o.error_code == "jsonl_missing"


def test_outcome_chunks_indexed_override():
    o = IngestionOutcome(rag_status="success", chunks_indexed=42)
    assert o.chunks_indexed == 42


def test_outcome_is_immutable():
    o = IngestionOutcome(rag_status="success", chunks_indexed=5)
    with pytest.raises(FrozenInstanceError):
        o.rag_status = "failed"  # type: ignore[misc]


def test_outcome_rejects_unknown_status():
    with pytest.raises(ValueError):
        IngestionOutcome(rag_status="bogus")


def test_outcome_rejects_negative_chunks():
    with pytest.raises(ValueError):
        IngestionOutcome(rag_status="success", chunks_indexed=-1)