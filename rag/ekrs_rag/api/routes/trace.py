"""POST /v1/constraints/trace — retrieve events for a trace_id from audit log.

Read-only over the audit log via AuditIndex. No new audit writes.
D8: scope_filter is a prefix match on event.scope_path.
A2: lineage_snapshot / conflict_details are optional; old traces return null.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from ekrs_rag.api.auth import require_parser_token
from ekrs_rag.observability.audit_index import AuditIndex

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["trace"])


class TraceRequest(BaseModel):
    trace_id: str = Field(..., min_length=1)
    scope_filter: str | None = None


def _get_audit_index(request: Request) -> AuditIndex | None:
    return getattr(request.app.state, "audit_index", None)


@router.post("/constraints/trace")
def constraints_trace(
    body: TraceRequest,
    request: Request,
    _auth: None = Depends(require_parser_token),
) -> dict[str, Any]:
    """Read-only trace retrieval. No new audit event written."""
    idx = _get_audit_index(request)
    if idx is None:
        return {
            "trace_id": body.trace_id,
            "events": [],
            "lineage_snapshot": None,
            "conflict_details": None,
        }

    lines = idx.seek(body.trace_id) or []
    if body.scope_filter:
        prefix = body.scope_filter
        lines = [l for l in lines if (l.raw.get("scope_path") or "").startswith(prefix)]

    events = [
        {"event": l.event, "trace_id": l.trace_id, "offset": l.offset, "raw": l.raw}
        for l in lines
    ]

    # D5: lineage_snapshot + conflict_details are pulled from the
    # `constraint_solve_started` event's payload (the canonical write site).
    # Fallback to first event for pre-6A traces that lack a solve_started event.
    snapshot: str | None = None
    details: list | None = None
    start_event = next(
        (e["raw"] for e in events if e["event"] == "constraint_solve_started"),
        None,
    )
    if start_event is not None:
        snapshot = start_event.get("lineage_snapshot")
        details = start_event.get("conflict_details")
    elif events:
        # Pre-Phase 6A back-compat (A2): field absent on older audit entries.
        first_raw = events[0]["raw"]
        snapshot = first_raw.get("lineage_snapshot")
        details = first_raw.get("conflict_details")
    return {
        "trace_id": body.trace_id,
        "events": events,
        "lineage_snapshot": snapshot,
        "conflict_details": details,
    }
