"""Tests for scope-aware ranking in EKRSRetriever.

TC_SCOPE_RANK_01: national scope ranks above project
TC_SCOPE_RANK_02: industry scope ranks above enterprise
TC_SCOPE_RANK_03: No active_scope still ranks by scope priority
TC_SCOPE_RANK_04: Composite score = vec * (1 + scope/100)
"""
from __future__ import annotations

import pytest

from ekrs_shared.models import Chunk

from ekrs_rag.retrieval.retriever import EKRSRetriever, _SCOPE_PRIORITY_MAP


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def national_chunk() -> Chunk:
    return Chunk(
        text="Temperature shall not exceed 80°C",
        scope_path=["national", "GB"],
        source_block_ids=["b1"],
        token_count=10,
        doc_hash="national-hash",
        version=1,
        page_numbers=[1],
        numeric_hints=[],
    )


@pytest.fixture
def industry_chunk() -> Chunk:
    return Chunk(
        text="Temperature shall not exceed 75°C",
        scope_path=["industry", "automotive"],
        source_block_ids=["b2"],
        token_count=10,
        doc_hash="industry-hash",
        version=1,
        page_numbers=[1],
        numeric_hints=[],
    )


@pytest.fixture
def enterprise_chunk() -> Chunk:
    return Chunk(
        text="Temperature shall not exceed 70°C",
        scope_path=["enterprise", "company-a"],
        source_block_ids=["b3"],
        token_count=10,
        doc_hash="enterprise-hash",
        version=1,
        page_numbers=[1],
        numeric_hints=[],
    )


@pytest.fixture
def project_chunk() -> Chunk:
    return Chunk(
        text="Temperature shall not exceed 65°C",
        scope_path=["project", "alpha"],
        source_block_ids=["b4"],
        token_count=10,
        doc_hash="project-hash",
        version=1,
        page_numbers=[1],
        numeric_hints=[],
    )


@pytest.fixture
def reference_chunk() -> Chunk:
    return Chunk(
        text="Temperature shall not exceed 60°C",
        scope_path=["reference", "external"],
        source_block_ids=["b5"],
        token_count=10,
        doc_hash="reference-hash",
        version=1,
        page_numbers=[1],
        numeric_hints=[],
    )


# =============================================================================
# TC_SCOPE_RANK_01: national scope ranks above project
# =============================================================================


def test_national_scope_ranks_above_project(project_chunk, national_chunk):
    """national scope priority (100) > project scope priority (40)."""
    # Same vector score, different scope -> national should rank higher
    chunks = [project_chunk, national_chunk]
    vec_scores = [1.0, 1.0]

    retriever = EKRSRetriever(qdrant=None)
    sorted_chunks, sorted_vec, scope_scores, final_scores = retriever._rank_by_scope(
        chunks, vec_scores
    )

    assert sorted_chunks[0] == national_chunk
    assert sorted_chunks[1] == project_chunk
    assert scope_scores[0] == 1.0  # national = 100/100
    assert scope_scores[1] == 0.4  # project = 40/100


# =============================================================================
# TC_SCOPE_RANK_02: industry scope ranks above enterprise
# =============================================================================


def test_industry_scope_ranks_above_enterprise(enterprise_chunk, industry_chunk):
    """industry scope priority (80) > enterprise scope priority (60)."""
    chunks = [enterprise_chunk, industry_chunk]
    vec_scores = [1.0, 1.0]

    retriever = EKRSRetriever(qdrant=None)
    sorted_chunks, sorted_vec, scope_scores, final_scores = retriever._rank_by_scope(
        chunks, vec_scores
    )

    assert sorted_chunks[0] == industry_chunk
    assert sorted_chunks[1] == enterprise_chunk


# =============================================================================
# TC_SCOPE_RANK_03: No active_scope still ranks by scope priority
# =============================================================================


def test_no_active_scope_ranks_by_priority(
    national_chunk, industry_chunk, project_chunk
):
    """Even without active_scope filtering, scope priority affects ranking."""
    chunks = [project_chunk, industry_chunk, national_chunk]
    vec_scores = [1.0, 1.0, 1.0]  # Equal vector scores

    retriever = EKRSRetriever(qdrant=None)
    sorted_chunks, sorted_vec, scope_scores, final_scores = retriever._rank_by_scope(
        chunks, vec_scores
    )

    # Order should be: national (100) > industry (80) > project (40)
    assert sorted_chunks[0] == national_chunk
    assert sorted_chunks[1] == industry_chunk
    assert sorted_chunks[2] == project_chunk


# =============================================================================
# TC_SCOPE_RANK_04: Composite score = vec * (1 + scope/100)
# =============================================================================


def test_composite_score_calculation(national_chunk, project_chunk):
    """final = vec * (1 + scope/100)."""
    # national: 1.0 * (1 + 100/100) = 1.0 * 2.0 = 2.0
    # project: 1.0 * (1 + 40/100) = 1.0 * 1.4 = 1.4
    chunks = [project_chunk, national_chunk]
    vec_scores = [1.0, 1.0]

    retriever = EKRSRetriever(qdrant=None)
    sorted_chunks, sorted_vec, scope_scores, final_scores = retriever._rank_by_scope(
        chunks, vec_scores
    )

    assert final_scores[0] == 2.0  # national
    assert final_scores[1] == 1.4  # project
    # Verify scope_scores match _SCOPE_PRIORITY_MAP / 100
    assert scope_scores[0] == pytest.approx(1.0)  # national = 100/100
    assert scope_scores[1] == pytest.approx(0.4)  # project = 40/100


def test_composite_score_with_different_vector_scores(national_chunk, project_chunk):
    """Higher vector score with lower scope can beat higher scope with lower vec."""
    # national: 0.5 * 2.0 = 1.0
    # project: 1.0 * 1.4 = 1.4  -> project wins
    chunks = [national_chunk, project_chunk]
    vec_scores = [0.5, 1.0]  # national has lower vec score

    retriever = EKRSRetriever(qdrant=None)
    sorted_chunks, sorted_vec, scope_scores, final_scores = retriever._rank_by_scope(
        chunks, vec_scores
    )

    # project should win because 1.0*1.4 > 0.5*2.0
    assert sorted_chunks[0] == project_chunk
    assert sorted_chunks[1] == national_chunk
    assert final_scores[0] == pytest.approx(1.4)  # project: 1.0 * 1.4
    assert final_scores[1] == pytest.approx(1.0)  # national: 0.5 * 2.0


# =============================================================================
# Edge cases
# =============================================================================


def test_empty_chunks():
    """Empty input returns empty lists."""
    retriever = EKRSRetriever(qdrant=None)
    chunks, vec, scope, final = retriever._rank_by_scope([], [])
    assert chunks == []
    assert vec == []
    assert scope == []
    assert final == []


def test_unknown_scope_defaults_to_project():
    """Unknown scope prefixes default to project priority (40)."""
    chunk = Chunk(
        text="Temperature shall not exceed 80°C",
        scope_path=["unknown_type", "thing"],
        source_block_ids=["b1"],
        token_count=10,
        doc_hash="hash",
        version=1,
        page_numbers=[1],
        numeric_hints=[],
    )

    retriever = EKRSRetriever(qdrant=None)
    priority = retriever._scope_priority(chunk)

    assert priority == pytest.approx(0.4)  # project = 40/100


def test_empty_scope_path_defaults_to_zero():
    """Empty scope_path returns 0 priority."""
    chunk = Chunk(
        text="Temperature shall not exceed 80°C",
        scope_path=[],
        source_block_ids=["b1"],
        token_count=10,
        doc_hash="hash",
        version=1,
        page_numbers=[1],
        numeric_hints=[],
    )

    retriever = EKRSRetriever(qdrant=None)
    priority = retriever._scope_priority(chunk)

    assert priority == 0.0
