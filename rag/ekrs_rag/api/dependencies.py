"""FastAPI dependencies (Phase 6A)."""
from __future__ import annotations

from fastapi import Request

from ekrs_rag.storage.documents import DocumentRepo


def get_document_repo(request: Request) -> DocumentRepo:
    """Retrieve the lifespan-initialized DocumentRepo from app.state."""
    repo = getattr(request.app.state, "document_repo", None)
    if repo is None:
        raise RuntimeError("DocumentRepo not initialized; check main.py lifespan")
    return repo
