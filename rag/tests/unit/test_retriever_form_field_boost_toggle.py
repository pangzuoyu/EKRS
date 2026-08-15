"""Phase 12 §七 Item 3 RED→GREEN: form_field/column_header boost toggle.

Q3 §9.6 last-mile validation requires comparing form_field boost ON vs OFF
to measure R4 retrieval improvement. Adds a `form_field_boost` kwarg to
``EKRSRetriever.retrieve()`` and ``EKRSRetriever._scope_priority()`` so
the §七 Item 3 baseline script can run both rounds without env-var flakiness.

Toggle semantics (from plan §七 Item 3):
- ``form_field_boost=True`` (default, post-Phase 12 T4 behavior preserved):
  apply ``max(base, FORM_FIELD_WEIGHT)`` for form_fields + same for
  column_headers (current T4 behavior).
- ``form_field_boost=False``: skip the form_field/column_header max —
  return base only (legacy T3 scope_path[0]-only behavior for the
  comparison baseline).

This file is RED-tests-only at first commit. Implementation lands in
``retriever.py`` as the GREEN follow-up. Coverage: 100% for the new
kwarg paths (single conditional inside _scope_priority).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from ekrs_shared.models import Chunk
from ekrs_rag.retrieval.retriever import (
    EKRSRetriever,
    FORM_FIELD_WEIGHT,
    COLUMN_HEADER_WEIGHT,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def retriever() -> EKRSRetriever:
    """Retriever stub. _scope_priority is the unit under test; no qdrant I/O."""
    return EKRSRetriever(qdrant=MagicMock())


# ---------------------------------------------------------------------------
# _scope_priority kwarg tests (RED → GREEN)
# ---------------------------------------------------------------------------


def test_scope_priority_toggle_false_skips_form_field_max(retriever) -> None:
    """form_field_boost=False: industry chunk with form_fields returns base=0.8 only.

    Without boost, _scope_priority returns the scope_path[0]-derived base
    (industry=0.8) — NOT max(base, FORM_FIELD_WEIGHT=0.9). This is the
    §七 Item 3 "boost OFF" baseline round.
    """
    chunk = Chunk(
        text="industry checklist",
        scope_path=["industry", "HG/T 20592"],
        form_fields=[{"key": "LOT NO", "value": "Lot 49"}],
    )
    score = EKRSRetriever._scope_priority(chunk, form_field_boost=False)
    assert score == pytest.approx(0.8)


def test_scope_priority_toggle_false_skips_column_header_max(retriever) -> None:
    """form_field_boost=False: reference chunk with column_headers returns base=0.2 only."""
    chunk = Chunk(
        text="reference table",
        scope_path=["reference"],
        column_headers=[{"index": 0, "header": "A105"}],
    )
    score = EKRSRetriever._scope_priority(chunk, form_field_boost=False)
    assert score == pytest.approx(0.2)


def test_scope_priority_toggle_true_keeps_form_field_max(retriever) -> None:
    """form_field_boost=True (default-equivalent explicit): industry chunk
    with form_fields returns max(0.8, 0.9) = 0.9. Preserves Phase 12 T4 behavior.
    """
    chunk = Chunk(
        text="industry checklist",
        scope_path=["industry", "HG/T 20592"],
        form_fields=[{"key": "LOT NO", "value": "Lot 49"}],
    )
    score = EKRSRetriever._scope_priority(chunk, form_field_boost=True)
    assert score == pytest.approx(0.9)


def test_scope_priority_toggle_true_keeps_column_header_max(retriever) -> None:
    """form_field_boost=True: reference + column_headers → max(0.2, 0.7) = 0.7."""
    chunk = Chunk(
        text="reference table",
        scope_path=["reference"],
        column_headers=[{"index": 0, "header": "A105"}],
    )
    score = EKRSRetriever._scope_priority(chunk, form_field_boost=True)
    assert score == pytest.approx(0.7)


def test_scope_priority_toggle_false_no_form_no_column_unchanged(retriever) -> None:
    """No form_fields / column_headers → boost OFF == boost ON (legacy chunks)."""
    chunk = Chunk(
        text="project chunk",
        scope_path=["project"],
    )
    off = EKRSRetriever._scope_priority(chunk, form_field_boost=False)
    on = EKRSRetriever._scope_priority(chunk, form_field_boost=True)
    assert off == on == pytest.approx(0.4)


def test_scope_priority_default_keeps_form_field_max(retriever) -> None:
    """Default (no kwarg) preserves Phase 12 T4 behavior — backward compat."""
    chunk = Chunk(
        text="industry checklist",
        scope_path=["industry"],
        form_fields=[{"key": "K", "value": "V"}],
    )
    # No kwarg — must remain identical to legacy T4 path
    score = EKRSRetriever._scope_priority(chunk)
    assert score == pytest.approx(0.9)
