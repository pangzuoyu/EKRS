"""POST /v1/calculate — direct constraint solve without Qdrant retrieval.

Spec §5 (D4): admin-only, reuses the same ConstraintV2 schema and solver
as /v1/constraints. D3: strict mode disables soft fallback.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from ekrs_shared.models import Constraint

from ekrs_rag.constraint_engine.solver import (
    IntervalSolver,
    StrictViolationError,
)
from ekrs_rag.observability.audit import get_writer
from ekrs_rag.security import require_admin_key

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["calculate"])

# PF2: module-level singleton — IntervalSolver is stateless (R2 pure function).
# Reusing one instance avoids per-request allocation.
solver = IntervalSolver()


class CalculateRequest(BaseModel):
    constraints: list[Constraint] = Field(..., min_length=0)
    # Q2: Literal restricts the op field at the type level; no runtime check needed.
    op: Literal["intersect"] = "intersect"
    scope_path: str = Field(..., min_length=1)
    strict: bool = True
    allow_soft_fallback: bool = True


@router.post("/calculate")
def calculate(
    body: CalculateRequest,
    _admin: None = Depends(require_admin_key),
) -> dict[str, Any]:
    """Direct solve. Skips retrieval. Audits with lineage_snapshot + conflict_details."""
    started = time.time()
    # Capture input snapshot for lineage (truncated, PF3)
    lineage_snapshot_raw = str([c.model_dump() for c in body.constraints])
    lineage_snapshot = (
        lineage_snapshot_raw[:4096] + "...[truncated]"
        if len(lineage_snapshot_raw) > 4096
        else lineage_snapshot_raw
    )
    conflict_details: list[dict[str, Any]] = []

    try:
        result = solver.solve_with_fallback(
            body.constraints,
            allow_soft_fallback=body.allow_soft_fallback,
            strict=body.strict,
        )
    except StrictViolationError as e:
        # Audit the failure with D7 duration_ms
        duration_ms = int((time.time() - started) * 1000)
        writer = get_writer()
        if writer is not None:
            writer.write(
                "constraint_solve_failed",
                trace_id="",
                error_type="strict_violation",
                duration_ms=duration_ms,
                lineage_snapshot=lineage_snapshot,
            )
        raise HTTPException(status_code=400, detail=f"strict_violation: {e}")

    # Convert _ParameterResult → JSON-safe shape
    branches: list[dict[str, Any]] = []
    for param, pres in result.items():
        branches.append({
            "parameter": param,
            "interval": str(pres.interval),
            "unit": pres.unit,
            "confidence": pres.confidence,
            "had_conflict": pres.had_conflict,
        })
        if pres.had_conflict:
            conflict_details.append({"parameter": param, "type": "soft_fallback"})

    # Audit (D7: emit duration_ms on every event from this endpoint)
    duration_ms = int((time.time() - started) * 1000)
    writer = get_writer()
    if writer is not None:
        writer.write(
            "constraint_solved",
            trace_id="",
            branches_count=len(branches),
            duration_ms=duration_ms,
            lineage_snapshot=lineage_snapshot,
            conflict_details=conflict_details or None,
        )

    return {
        "success": True,
        "data": {
            "branches": branches,
            "lineage_snapshot": lineage_snapshot,
            "conflict_details": conflict_details or None,
        },
        "error": None,
    }
