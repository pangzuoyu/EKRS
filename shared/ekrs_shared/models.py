"""EKRS shared Pydantic models.

Mirrors doc-to-md DocumentBlock IR schema and adds RAG-specific types.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any, List, Optional, Tuple, Union

from pydantic import BaseModel, Field


# --- DocumentBlock IR (mirrors doc-to-md shared/schema.py) ---


class Content(BaseModel):
    raw: str = ""
    structured: Optional[Any] = None
    formulas: Optional[Any] = None
    md_preview: str = ""


class Metadata(BaseModel):
    page_number: int = Field(default=1, ge=1)
    bbox: Optional[List[float]] = None
    heading_path: Optional[List[str]] = None


class Lineage(BaseModel):
    parser_version: str = ""
    strategy: str = ""
    steps: List[str] = Field(default_factory=list)


class DocumentBlockIR(BaseModel):
    """Full DocumentBlock IR from doc-to-md parser output.

    Each line in data.jsonl is one DocumentBlockIR object.
    """

    doc_id: str
    block_id: str
    type: str  # header|text|table|kv|attachment_ref
    content: Content = Field(default_factory=Content)
    metadata: Metadata = Field(default_factory=Metadata)
    lineage: Lineage = Field(default_factory=Lineage)
    uncertainty_score: float = 0.0


# --- RAG-specific models ---


class NumericHint(BaseModel):
    """Lightweight numeric anchor extracted at ingestion time.

    R1: Must always carry source_span, block_id, source_text.
    No operator extraction, no parameter normalization at this stage.
    """

    parameter_hint: str = ""  # raw text fragment, not normalized
    value: float
    unit: str
    span: Tuple[int, int]  # (start, end) relative to chunk.text
    source_text: str = ""
    block_id: str = ""
    page_num: Optional[int] = None
    scope_path: Optional[List[str]] = None


class Priority(IntEnum):
    NATIONAL = 100  # 国标
    INDUSTRY = 80  # 行标
    ENTERPRISE = 60  # 企标
    PROJECT = 40  # 项目/合同
    REFERENCE = 20  # 参考


class Condition(BaseModel):
    parameter: str
    operator: str  # ==, >, <, contains
    value: Any


class Constraint(BaseModel):
    """A single constraint on a parameter.

    R2: Constraints are pure data objects consumed by the pure-function solver.
    """

    parameter: str
    operator: str  # <=, >=, ==, range
    value: Union[float, Tuple[float, float]]
    unit: str
    category: str = "general"
    priority: Priority = Priority.PROJECT
    confidence: float = 1.0
    conditions: List[Condition] = Field(default_factory=list)
    source: dict = Field(default_factory=dict)
    scope_path: Optional[List[str]] = None
    version: Optional[int] = None
    content_hash: Optional[str] = None


class Evidence(BaseModel):
    """Tracks provenance from source document to solved constraint."""

    doc_id: str
    block_id: str
    page_num: Optional[int] = None
    scope_path: Optional[List[str]] = None
    source_text: str = ""
    span: Tuple[int, int] = (0, 0)


class Chunk(BaseModel):
    """Semantic chunk produced by the ingestion chunker.

    One chunk = one Qdrant point.
    """

    text: str
    scope_path: List[str] = Field(default_factory=list)
    source_block_ids: List[str] = Field(default_factory=list)
    token_count: int = 0
    doc_hash: str = ""
    version: int = 0
    page_numbers: List[int] = Field(default_factory=list)
    numeric_hints: List[NumericHint] = Field(default_factory=list)


# --- API request/response models ---


class IngestionNotification(BaseModel):
    """Payload from parser to RAG via POST /v1/ingestion/notify."""

    trace_id: str = ""
    doc_hash: str
    version: int
    output_path: str
    callback_url: str = ""
    metadata: Optional[dict] = None


class IngestionStatus(BaseModel):
    """Response from GET /v1/ingestion/status/{doc_hash}."""

    status: str  # processing|success|failed
    chunks_indexed: int = 0
    version: int = 0
    error: Optional[str] = None
