"""EKRS shared Pydantic models.

Mirrors doc-to-md DocumentBlock IR schema and adds RAG-specific types.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any, List, Literal, Optional, Tuple, Union

from pydantic import BaseModel, Field, field_validator


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


class ConstraintV2(BaseModel):
    """A single constraint on a parameter (IR V2).

    R2: Constraints are pure data objects consumed by the pure-function solver.
    """

    parameter: str
    value_type: Literal["interval", "enum", "scalar", "boolean"] = "scalar"
    unit: str = ""
    category: str = "general"

    # Interval bounds (used when value_type == "interval")
    interval: Optional[dict] = None  # {lower, upper, lower_inclusive, upper_inclusive}

    # Scalar value (used when value_type == "scalar")
    scalar_value: Optional[float] = None

    # Enum values (used when value_type == "enum")
    enum_values: Optional[List[str]] = None

    # Boolean value (used when value_type == "boolean")
    boolean_value: Optional[bool] = None

    # Priority restructured: separate explicit_level from recency/authority scores
    priority: dict = Field(default_factory=dict)  # {explicit_level, recency_score, authority_score}

    # Confidence
    confidence: float = 1.0

    # Inferred flag: True if constraint is inferred from context
    inferred: bool = False

    # Lifecycle (elevated from content_hash/version)
    lifecycle: dict = Field(default_factory=dict)  # {status, effective_date, expiry_date, is_binding}

    # Source restructured: flat structure
    source: dict = Field(default_factory=dict)  # {doc_id, provision_id, doc_type, authority_score}

    # Conditions for conditional constraints
    conditions: List[Condition] = Field(default_factory=list)

    # Scope path for filtering
    scope_path: Optional[List[str]] = None


class Constraint(BaseModel):
    """A single constraint on a parameter (IR V1 — kept for reference during migration).

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

    @field_validator("priority", mode="before")
    @classmethod
    def _accept_priority_name(cls, v: Any) -> Any:
        """Accept string enum names (e.g. "NATIONAL") for JSON API callers.

        Pydantic v2 IntEnum rejects string names by default; this lets
        /v1/calculate receive `"priority": "NATIONAL"` from external
        callers without breaking in-process users that pass `Priority.NATIONAL`.
        """
        if isinstance(v, str):
            try:
                return Priority[v]
            except KeyError as e:
                raise ValueError(
                    f"priority must be one of {[p.name for p in Priority]}, "
                    f"got {v!r}"
                ) from e
        return v


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
