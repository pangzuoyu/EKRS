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


def test_outcome_duplicate_idempotency_short_circuit():
    """Phase 7 T3: reparse() returns 'duplicate' when SHA256 already matches.

    Maintained alongside `success` and `failed` so the dataclass type
    system mirrors the four legitimate pipeline outcomes.
    """
    o = IngestionOutcome(
        rag_status="duplicate", chunks_indexed=42, error_code="idempotent_skip",
    )
    assert o.rag_status == "duplicate"
    assert o.chunks_indexed == 42
    assert o.error_code == "idempotent_skip"


def test_outcome_business_failure_distinct_from_infra_failure():
    """Phase 7 T3: reparse() returns 'business_failure' for ops errors.

    Distinguished from 'failed' (infra-level): JSONL missing,
    source_path missing, parse errors. Operators route these
    differently in the parser-side compensation logic.
    """
    o = IngestionOutcome(
        rag_status="business_failure",
        error="source_path missing: /var/parser_out/x.jsonl",
        error_code="source_missing",
    )
    assert o.rag_status == "business_failure"
    assert o.error_code == "source_missing"
    assert o.error is not None and "source_path" in o.error