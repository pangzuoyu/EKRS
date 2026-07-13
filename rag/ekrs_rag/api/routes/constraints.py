"""Constraints API route.

POST /v1/constraints — query engineering constraints via the three-gate pipeline:
  Gate 1 (Recall):    retrieval returns < MIN_RECALL_CHUNKS → 404
  Gate 2 (Extract):  no constraints extracted → 404
  Gate 3 (Solve):    solver reports CONFLICT → 200 + conflict in response
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, model_validator

from ekrs_rag.api.auth import require_parser_token
from ekrs_rag.constraint_engine.evidence_builder import EvidenceBuilder
from ekrs_rag.constraint_engine.solver import IntervalSolver
from ekrs_rag.observability.audit_index import AuditIndex
from ekrs_rag.observability.metrics import METRICS, safe_inc
from ekrs_rag.observability.trace import get_trace_id
from ekrs_rag.retrieval.retriever import EKRSRetriever, RetrievalResult

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["constraints"])

# Gate threshold
MIN_RECALL_CHUNKS = 1

# Module-level retriever, set by main.py at startup
_retriever: Optional[EKRSRetriever] = None


def set_retriever(retriever: EKRSRetriever) -> None:
    """Inject the retriever instance (called at startup)."""
    global _retriever
    _retriever = retriever


# Module-level audit index, set by main.py at startup (Phase 5 Query Replay)
_audit_index: Optional[AuditIndex] = None


def set_audit_index(index: AuditIndex) -> None:
    """Inject audit index (called at startup)."""
    global _audit_index
    _audit_index = index


# ---------------------------------------------------------------------------
# Dependency functions
# ---------------------------------------------------------------------------


def get_retriever(request: Request) -> EKRSRetriever:
    """Strict dep: read retriever from app.state. 503 if uninitialized."""
    r = getattr(request.app.state, "retriever", None)
    if r is None:
        raise HTTPException(status_code=503, detail="retriever not initialized")
    return r


def get_audit_index(request: Request) -> AuditIndex | None:
    """Optional dep: returns AuditIndex or None. Replay branch checks None."""
    return getattr(request.app.state, "audit_index", None)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class ConstraintQuery(BaseModel):
    """Query payload for /v1/constraints."""

    query: str
    context: dict = {}
    strict: bool = False
    replay: bool = False
    replay_trace_id: str | None = None
    trace_id: str | None = None
    top_k: int = 40


class ConstraintQueryResponse(BaseModel):
    """Response from /v1/constraints."""

    branches: dict  # {"general": {...}, "高温环境": {...}}
    primary_branch: str | None = None  # "general" or branch key
    conflicts: list[dict] = []
    trace: list[dict] = []
    mode: str  # "single" or "multi_branch"
    deterministic_match: bool | None = None  # only set on replay responses

    @model_validator(mode="after")
    def _validate_primary_branch(self) -> "ConstraintQueryResponse":
        if self.primary_branch is not None and self.primary_branch not in self.branches:
            raise ValueError(
                f"primary_branch '{self.primary_branch}' must be one of the branch keys: {list(self.branches.keys())}"
            )
        return self


# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------


@router.post("/constraints", response_model=ConstraintQueryResponse)
async def query_constraints(
    query: ConstraintQuery,
    request: Request,
    _auth: None = Depends(require_parser_token),
) -> ConstraintQueryResponse:
    """Query engineering constraints using the three-gate pipeline.

    Flow:
      1. Context merge (placeholder — doc/inferred contexts empty for Phase 2b)
      2. Retrieval via EKRSRetriever
      3. Gate 1: insufficient recall → 404
      4. Evidence building via EvidenceBuilder
      5. Gate 2: no constraints extracted → 404
      6. Solving via IntervalSolver
      7. Gate 3: CONFLICT → 200 + conflict in response
      8. Return structured result

    Phase 5 Replay branch: when query.replay=True, look up the prior
    solve via the AuditIndex keyed on query.replay_trace_id, re-run
    the solver with the stored query/scope, and report whether the
    branches_count matches (deterministic_match).

    Note: replay's pre-flight checks (missing/id trace_id, audit_index
    not initialized, unknown trace_id) are evaluated BEFORE retriever
    acquisition so they can return 4xx without a running retrieval
    pipeline.
    """
    # --- Replay branch (Phase 5): pre-flight checks BEFORE retriever ---
    if query.replay:
        if not query.replay_trace_id:
            raise HTTPException(status_code=400, detail="replay_trace_id required")
        if _audit_index is None:
            raise HTTPException(status_code=503, detail="audit index not initialized")

        prior_lines = _audit_index.seek(query.replay_trace_id)
        if prior_lines is None:
            raise HTTPException(status_code=400, detail="no_prior_solve")

        # Extract prior query inputs
        prior_started = next((l for l in prior_lines if l.event == "constraint_solve_started"), None)
        prior_solved = next((l for l in prior_lines if l.event == "constraint_solved"), None)
        if not prior_started or not prior_solved:
            raise HTTPException(status_code=400, detail="incomplete_prior_solve")

        # From here on the retriever is needed to re-fetch the prior query.
        retriever: EKRSRetriever = getattr(request.app.state, "retriever", None) or _retriever
        if retriever is None:
            # Replay mode but retrieval isn't available yet — same gate-1
            # behavior as the normal flow (insufficient recall → 404).
            raise HTTPException(status_code=404, detail="Insufficient recall")

        # Override inputs with prior values
        replay_query = prior_started.raw.get("query", query.query)
        replay_scope = prior_started.raw.get("scope_path", query.context.get("scope_path"))

        # Re-run solver with prior inputs (re-fetch retrieval)
        retrieval_result: RetrievalResult = retriever.retrieve(
            replay_query, top_k=query.top_k, active_scope=replay_scope,
        )
        constraints = EvidenceBuilder.build(retrieval_result.chunks)
        result = IntervalSolver.solve(constraints, active_scope=replay_scope)

        # Compare with prior
        prior_branches = prior_solved.raw.get("branches_count", 0)
        new_branches = len(result.get("branches", {}))
        deterministic_match = (prior_branches == new_branches)

        # Audit + metric
        from ekrs_rag.observability.audit import get_writer
        writer = get_writer()
        if writer:
            writer.write(
                "query_replay_executed",
                trace_id=get_trace_id(),
                replayed_trace_id=query.replay_trace_id,
                deterministic_match=deterministic_match,
            )
        safe_inc(METRICS.constraint_solve_total,
                 outcome="replay_match" if deterministic_match else "replay_mismatch")

        return ConstraintQueryResponse(
            branches=result.get("branches", {}),
            primary_branch=result.get("primary_branch"),
            conflicts=result.get("conflicts", []),
            trace=result.get("trace", []),
            mode="multi_branch" if replay_scope else "single",
            deterministic_match=deterministic_match,
        )

    # --- Normal flow continues below (existing code) ---

    # Get retriever from app.state (set by main.py startup) or module-level fallback
    retriever: EKRSRetriever = getattr(request.app.state, "retriever", None) or _retriever
    if retriever is None:
        raise RuntimeError("Retriever not initialized")

    active_scope = query.context.get("scope_path")

    # --- Gate 0: context merge (placeholder, doc/inferred contexts empty) ---
    # In Phase 2b doc_context and inferred_context are empty dicts.
    # The user_context is query.context and is passed through as-is.
    merged_context = _context_merge(query.context, {}, {})

    # --- Step 2: Retrieval ---
    retrieval_result: RetrievalResult = retriever.retrieve(
        query.query,
        top_k=query.top_k,
        active_scope=active_scope,
    )

    # --- Gate 1: Recall check ---
    if len(retrieval_result.chunks) < MIN_RECALL_CHUNKS:
        logger.warning("Gate 1 trigger: insufficient recall chunks=%d", len(retrieval_result.chunks))
        raise HTTPException(status_code=404, detail="Insufficient recall")

    # --- Step 4: Evidence building ---
    constraints = EvidenceBuilder.build(retrieval_result.chunks)

    # --- Gate 2: Extraction check ---
    if len(constraints) == 0:
        logger.warning("Gate 2 trigger: no constraints extracted")
        raise HTTPException(status_code=404, detail="No constraints extracted")

    # --- Strict mode validation (R6) ---
    if query.strict:
        for c in constraints:
            if c.inferred:
                raise HTTPException(
                    status_code=400,
                    detail="missing_context: inferred constraint not allowed in strict mode",
                )

    # --- Step 6: Solving ---
    result = IntervalSolver.solve(constraints, active_scope=active_scope)

    # --- Gate 3: Conflict check ---
    conflicts: list[dict] = []
    if result["status"] == "CONFLICT":
        conflicts = result.get("conflicts", [])
        logger.info("Gate 3 CONFLICT: %d conflict(s)", len(conflicts))
        raise HTTPException(status_code=409, detail={"conflicts": conflicts})

    # Determine mode
    mode = "multi_branch" if active_scope else "single"

    return ConstraintQueryResponse(
        branches=result.get("branches", {}),
        primary_branch=result.get("primary_branch"),
        conflicts=conflicts,
        trace=result.get("trace", []),
        mode=mode,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _context_merge(
    user_context: dict,
    doc_context: dict,
    inferred_context: dict,
) -> dict:
    """Merge three context sources.

    Priority: User > Explicit_Doc > Inferred_Doc > Default.
    For Phase 2b, doc_context and inferred_context are empty dicts,
    so user_context is returned as-is.
    """
    # Placeholder implementation — merge logic to be defined in Phase 2c
    return user_context
