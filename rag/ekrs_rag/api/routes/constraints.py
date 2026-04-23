"""Constraints API route.

POST /v1/constraints — query engineering constraints via the three-gate pipeline:
  Gate 1 (Recall):    retrieval returns < MIN_RECALL_CHUNKS → 404
  Gate 2 (Extract):  no constraints extracted → 404
  Gate 3 (Solve):    solver reports CONFLICT → 200 + conflict in response
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel

from ekrs_rag.constraint_engine.evidence_builder import EvidenceBuilder
from ekrs_rag.constraint_engine.solver import IntervalSolver
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


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class ConstraintQuery(BaseModel):
    """Query payload for /v1/constraints."""

    query: str
    context: dict = {}
    strict: bool = False
    replay: bool = False
    trace_id: str | None = None
    top_k: int = 40


class ConstraintQueryResponse(BaseModel):
    """Response from /v1/constraints."""

    parameters: dict
    conflicts: list[dict] = []
    trace: list[dict] = []
    mode: str  # "single" or "multi_branch"


# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------


@router.post("/constraints", response_model=ConstraintQueryResponse)
async def query_constraints(query: ConstraintQuery, request: Request) -> ConstraintQueryResponse:
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
    """
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
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Insufficient recall")

    # --- Step 4: Evidence building ---
    constraints = EvidenceBuilder.build(retrieval_result.chunks)

    # --- Gate 2: Extraction check ---
    if len(constraints) == 0:
        logger.warning("Gate 2 trigger: no constraints extracted")
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="No constraints extracted")

    # --- Strict mode validation (R6) ---
    if query.strict:
        for c in constraints:
            if c.inferred:
                from fastapi import HTTPException
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
        from fastapi import HTTPException
        raise HTTPException(status_code=409, detail={"conflicts": conflicts})

    # Determine mode
    mode = "multi_branch" if active_scope else "single"

    return ConstraintQueryResponse(
        parameters=result.get("parameters", {}),
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
