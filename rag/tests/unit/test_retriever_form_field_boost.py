"""T4 RED→GREEN: retriever._scope_priority R4 form_field/column_header boost.

Per Phase 12 plan §三:
- T4: retriever._scope_priority extension. Phase 12 constants:
  FORM_FIELD_WEIGHT = 0.9, COLUMN_HEADER_WEIGHT = 0.7.
- _scope_priority(c) returns max(base, form_boost, column_boost).
- R6 strict parity: deterministic, no LLM/cross-encoder (parent §25).

PRR plan: docs/superpowers/plans/2026-08-14-phase12-form-field-r4-boost.md §三
"""

from __future__ import annotations

import pytest

from ekrs_shared.models import Chunk


def _scope_priority(retriever, chunk: Chunk) -> float:
    return retriever._scope_priority(chunk)


@pytest.fixture
def retriever():
    from ekrs_rag.retrieval.retriever import EKRSRetriever

    # EKRSRetriever doesn't need a real QdrantManager for _scope_priority —
    # only the static method is exercised. Use a stub for the constructor.
    class _Stub:
        pass

    # Provide a QdrantManager stand-in (None will fail type check) — easier to
    # use the real class with a Mock.
    from unittest.mock import MagicMock

    return EKRSRetriever(qdrant=MagicMock())


def test_scope_priority_form_field_overrides_national(retriever) -> None:
    """T4: form_fields present → scope_priority = max(base=1.0, 0.9) = 1.0.

    National scope already gives base=1.0; form_field boost (0.9) doesn't
    exceed it. R4 deterministic priority order preserved.
    """
    chunk = Chunk(
        text="LOT 49 CHECKLIST",
        scope_path=["national", "GB/T 12459"],
        form_fields=[{"key": "SYSTEM NO", "value": "Lot 49"}],
    )
    score = _scope_priority(retriever, chunk)
    assert score == 1.0


def test_scope_priority_form_field_lifts_industry_to_090(retriever) -> None:
    """T4: industry base=0.8 + form_fields → max(0.8, 0.9) = 0.9."""
    chunk = Chunk(
        text="industry checklist",
        scope_path=["industry", "HG/T 20592"],
        form_fields=[{"key": "K", "value": "V"}],
    )
    score = _scope_priority(retriever, chunk)
    assert score == 0.9


def test_scope_priority_column_header_lifts_reference_to_070(retriever) -> None:
    """T4: reference base=0.2 + column_headers → max(0.2, 0.7) = 0.7."""
    chunk = Chunk(
        text="reference table",
        scope_path=["reference"],
        column_headers=[{"index": 0, "header": "Item"}],
    )
    score = _scope_priority(retriever, chunk)
    assert score == 0.7


def test_scope_priority_both_fields_takes_max(retriever) -> None:
    """T4: form_fields (0.9) > column_headers (0.7) → returns 0.9."""
    chunk = Chunk(
        text="multi-feature block",
        scope_path=["project"],
        form_fields=[{"key": "K", "value": "V"}],
        column_headers=[{"index": 0, "header": "Item"}],
    )
    score = _scope_priority(retriever, chunk)
    assert score == 0.9


def test_scope_priority_no_form_no_column_uses_base(retriever) -> None:
    """T4: legacy chunk (no form_fields / column_headers) → base scope only."""
    chunk = Chunk(
        text="legacy chunk",
        scope_path=["project"],
    )
    score = _scope_priority(retriever, chunk)
    assert score == 0.4  # project base = 40/100


def test_scope_priority_empty_scope_remains_zero(retriever) -> None:
    """T4: empty scope_path + form_fields → max(0.0, 0.9) = 0.9 (form_field still boosts)."""
    chunk = Chunk(
        text="form-only block",
        scope_path=[],
        form_fields=[{"key": "K", "value": "V"}],
    )
    score = _scope_priority(retriever, chunk)
    assert score == 0.9


def test_scope_priority_form_field_weight_is_module_constant(retriever) -> None:
    """T4: FORM_FIELD_WEIGHT and COLUMN_HEADER_WEIGHT live at module scope.

    Operators can tune via the constant (no-op default); tests guard against
    accidental removal.
    """
    from ekrs_rag.retrieval import retriever as retriever_mod

    assert hasattr(retriever_mod, "FORM_FIELD_WEIGHT")
    assert hasattr(retriever_mod, "COLUMN_HEADER_WEIGHT")
    assert retriever_mod.FORM_FIELD_WEIGHT == 0.9
    assert retriever_mod.COLUMN_HEADER_WEIGHT == 0.7