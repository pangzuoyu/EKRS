"""Task C: chunk_blocks must stamp every produced Chunk with doc_type."""
import pytest

from ekrs_rag.ingestion.chunker import chunk_blocks
from ekrs_shared.models import Content, DocumentBlockIR, Lineage, Metadata


def _make_block(
    block_id: str = "block-1",
    text: str = "hello world",
    page_number: int = 1,
    heading_path: list[str] | None = None,
) -> DocumentBlockIR:
    return DocumentBlockIR(
        doc_id="doc-1",
        block_id=block_id,
        type="text",
        content=Content(raw=text, md_preview=text),
        metadata=Metadata(page_number=page_number, heading_path=heading_path),
        lineage=Lineage(),
    )


@pytest.mark.unit
def test_chunk_blocks_stamps_doc_type_on_every_chunk():
    """All Chunks produced carry the doc_type kwarg.

    Uses distinct heading_path per block so Boundary 2 (scope-change)
    fires and produces >= 2 chunks under default max_tokens — short
    same-scope blocks would otherwise merge into a single chunk.
    """
    blocks = [
        _make_block("b1", "alpha", heading_path=["section-1"]),
        _make_block("b2", "beta", heading_path=["section-2"]),
    ]
    chunks = chunk_blocks(blocks, doc_hash="abc", version=1, doc_type="national_standard")
    assert len(chunks) >= 2
    for c in chunks:
        assert c.doc_type == "national_standard"


@pytest.mark.unit
def test_chunk_blocks_default_doc_type_is_none():
    """Default doc_type=None preserves pre-Task-C byte-level behavior
    (golden set parity)."""
    blocks = [_make_block("b1", "alpha")]
    chunks = chunk_blocks(blocks, doc_hash="abc", version=1)
    for c in chunks:
        assert c.doc_type is None


@pytest.mark.unit
def test_chunk_blocks_doc_type_round_trips_through_split():
    """Multi-block group → split chunks all carry doc_type."""
    blocks = [_make_block(f"b{i}", f"block {i} " * 50) for i in range(5)]
    chunks = chunk_blocks(blocks, doc_hash="abc", version=1, doc_type="lot_checklist")
    assert len(chunks) >= 1
    for c in chunks:
        assert c.doc_type == "lot_checklist"
