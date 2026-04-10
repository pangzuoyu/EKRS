"""Semantic chunker — converts DocumentBlockIR list into Chunk objects.

Three boundary conditions (per design doc):
1. Scope change: heading_path differs → flush current chunk, start new
2. Table/kv type: standalone chunk, with header propagation on overflow
3. Token overflow: estimate via len/4, flush when exceeding max_tokens

Edge case: single block > max_tokens → split with warning log (not silent truncation).
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from ekrs_shared.models import Chunk, DocumentBlockIR

from .ir_parser import extract_text

logger = logging.getLogger(__name__)


def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token (Phase 1).

    Phase 2 replaces with precise tokenizer.
    """
    return max(1, len(text) // 4)


def extract_table_headers(block: DocumentBlockIR) -> list[str]:
    """Extract column headers from a table block.

    Tries content.structured (first row) first, then parses md_preview.
    Returns empty list if no headers found.
    """
    # Try structured data (list of lists, first row = headers)
    if block.content.structured and isinstance(block.content.structured, list):
        rows = block.content.structured
        if rows and isinstance(rows[0], list):
            return [str(cell) for cell in rows[0] if cell]

    # Fallback: parse md_preview for markdown table header row
    if block.content.md_preview:
        lines = block.content.md_preview.strip().split("\n")
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("|") and "---" not in stripped:
                cells = [c.strip() for c in stripped.split("|") if c.strip()]
                if cells:
                    return cells

    return []


def _get_scope_path(block: DocumentBlockIR) -> list[str]:
    """Get heading_path as a list (empty list if missing)."""
    return block.metadata.heading_path or []


def _flush_chunk(
    text_parts: list[str],
    scope_path: list[str],
    block_ids: list[str],
    doc_hash: str,
    version: int,
    page_numbers: list[int],
) -> Optional[Chunk]:
    """Build a Chunk from accumulated text parts. Returns None if empty."""
    text = "\n".join(text_parts).strip()
    if not text:
        return None

    return Chunk(
        text=text,
        scope_path=list(scope_path),
        source_block_ids=list(block_ids),
        token_count=estimate_tokens(text),
        doc_hash=doc_hash,
        version=version,
        page_numbers=sorted(set(page_numbers)),
    )


def _split_large_block(
    block: DocumentBlockIR,
    text: str,
    max_tokens: int,
    doc_hash: str,
    version: int,
    scope_path: list[str],
    page_numbers: list[int],
) -> list[Chunk]:
    """Split a single large block that exceeds max_tokens.

    For tables: propagate column headers to each sub-chunk.
    For text: split at sentence/line boundaries.
    """
    chunks: list[Chunk] = []

    if block.type == "table":
        headers = extract_table_headers(block)
        header_prefix = " | ".join(headers) + "\n" if headers else ""

        # Split by rows if structured data available
        if block.content.structured and isinstance(block.content.structured, list):
            rows = block.content.structured
            header_row = rows[0] if rows else []
            data_rows = rows[1:] if rows else []

            current_parts: list[str] = [header_prefix] if header_prefix else []
            current_tokens = estimate_tokens(header_prefix)
            row_block_ids: list[str] = []

            for row in data_rows:
                row_text = " | ".join(str(c) for c in row)
                row_tokens = estimate_tokens(row_text)

                if current_tokens + row_tokens > max_tokens and len(current_parts) > (1 if header_prefix else 0):
                    chunk_text = "\n".join(current_parts).strip()
                    if chunk_text:
                        chunks.append(Chunk(
                            text=chunk_text,
                            scope_path=list(scope_path),
                            source_block_ids=[block.block_id],
                            token_count=estimate_tokens(chunk_text),
                            doc_hash=doc_hash,
                            version=version,
                            page_numbers=list(page_numbers),
                        ))
                    current_parts = [header_prefix] if header_prefix else []
                    current_tokens = estimate_tokens(header_prefix)
                    row_block_ids = []

                current_parts.append(row_text)
                current_tokens += row_tokens
                row_block_ids.append(block.block_id)

            # Flush remaining
            if current_parts:
                chunk_text = "\n".join(current_parts).strip()
                if chunk_text:
                    chunks.append(Chunk(
                        text=chunk_text,
                        scope_path=list(scope_path),
                        source_block_ids=[block.block_id],
                        token_count=estimate_tokens(chunk_text),
                        doc_hash=doc_hash,
                        version=version,
                        page_numbers=list(page_numbers),
                    ))
        else:
            # No structured data: fall through to text-based splitting
            chunks.extend(_split_text(text, max_tokens, doc_hash, version, scope_path, [block.block_id], page_numbers))
    else:
        chunks.extend(_split_text(text, max_tokens, doc_hash, version, scope_path, [block.block_id], page_numbers))

    if not chunks:
        logger.warning(
            "Block %s produced no chunks after split (text length=%d)",
            block.block_id, len(text),
        )

    return chunks


def _split_text(
    text: str,
    max_tokens: int,
    doc_hash: str,
    version: int,
    scope_path: list[str],
    block_ids: list[str],
    page_numbers: list[int],
) -> list[Chunk]:
    """Split text at line or character boundaries when it exceeds max_tokens."""
    chunks: list[Chunk] = []
    lines = text.split("\n")
    current_parts: list[str] = []
    current_tokens = 0

    for line in lines:
        line_tokens = estimate_tokens(line)

        # If a single line exceeds max_tokens, split it by characters
        if line_tokens > max_tokens:
            # Flush current accumulated text first
            if current_parts:
                chunk_text = "\n".join(current_parts).strip()
                if chunk_text:
                    chunks.append(Chunk(
                        text=chunk_text,
                        scope_path=list(scope_path),
                        source_block_ids=list(block_ids),
                        token_count=estimate_tokens(chunk_text),
                        doc_hash=doc_hash,
                        version=version,
                        page_numbers=list(page_numbers),
                    ))
                current_parts = []
                current_tokens = 0

            # Split long line into character-based chunks (~4 chars per token)
            chars_per_chunk = max_tokens * 4
            for i in range(0, len(line), chars_per_chunk):
                segment = line[i:i + chars_per_chunk].strip()
                if segment:
                    chunks.append(Chunk(
                        text=segment,
                        scope_path=list(scope_path),
                        source_block_ids=list(block_ids),
                        token_count=estimate_tokens(segment),
                        doc_hash=doc_hash,
                        version=version,
                        page_numbers=list(page_numbers),
                    ))
            continue

        if current_tokens + line_tokens > max_tokens and current_parts:
            chunk_text = "\n".join(current_parts).strip()
            if chunk_text:
                chunks.append(Chunk(
                    text=chunk_text,
                    scope_path=list(scope_path),
                    source_block_ids=list(block_ids),
                    token_count=estimate_tokens(chunk_text),
                    doc_hash=doc_hash,
                    version=version,
                    page_numbers=list(page_numbers),
                ))
            current_parts = []
            current_tokens = 0

        current_parts.append(line)
        current_tokens += line_tokens

    if current_parts:
        chunk_text = "\n".join(current_parts).strip()
        if chunk_text:
            chunks.append(Chunk(
                text=chunk_text,
                scope_path=list(scope_path),
                source_block_ids=list(block_ids),
                token_count=estimate_tokens(chunk_text),
                doc_hash=doc_hash,
                version=version,
                page_numbers=list(page_numbers),
            ))

    return chunks


def chunk_blocks(
    blocks: list[DocumentBlockIR],
    doc_hash: str,
    version: int,
    max_tokens: int = 500,
) -> list[Chunk]:
    """Convert DocumentBlockIR list into Chunk objects.

    Semantic chunking with three boundary conditions:
    1. Scope change (heading_path differs)
    2. Table/kv → standalone chunk
    3. Token overflow → flush and start new

    Returns list of Chunk objects ready for Qdrant upsert.
    """
    if not blocks:
        return []

    chunks: list[Chunk] = []
    current_text_parts: list[str] = []
    current_scope: list[str] = []
    current_block_ids: list[str] = []
    current_tokens = 0
    current_pages: list[int] = []

    for block in blocks:
        text = extract_text(block)
        if not text:
            continue

        scope = _get_scope_path(block)
        page_num = block.metadata.page_number

        # Boundary 1: Table/kv → standalone chunk
        if block.type in ("table", "kv"):
            # Flush accumulated text chunk first
            chunk = _flush_chunk(
                current_text_parts, current_scope, current_block_ids,
                doc_hash, version, current_pages,
            )
            if chunk:
                chunks.append(chunk)
                current_text_parts = []
                current_block_ids = []
                current_tokens = 0
                current_pages = []

            # Create standalone chunk for table/kv
            block_tokens = estimate_tokens(text)
            if block_tokens > max_tokens:
                # Large block: split with header propagation
                logger.info(
                    "Large %s block %s (%d tokens), splitting",
                    block.type, block.block_id, block_tokens,
                )
                chunks.extend(
                    _split_large_block(block, text, max_tokens, doc_hash, version, scope, [page_num])
                )
            else:
                chunks.append(Chunk(
                    text=text,
                    scope_path=scope,
                    source_block_ids=[block.block_id],
                    token_count=block_tokens,
                    doc_hash=doc_hash,
                    version=version,
                    page_numbers=[page_num],
                ))

            current_scope = scope
            continue

        # Boundary 2: Scope change → flush
        if scope != current_scope:
            chunk = _flush_chunk(
                current_text_parts, current_scope, current_block_ids,
                doc_hash, version, current_pages,
            )
            if chunk:
                chunks.append(chunk)
            current_text_parts = []
            current_block_ids = []
            current_tokens = 0
            current_pages = []
            current_scope = scope

        # Boundary 3: Token overflow
        text_tokens = estimate_tokens(text)
        if current_tokens + text_tokens > max_tokens and current_text_parts:
            chunk = _flush_chunk(
                current_text_parts, current_scope, current_block_ids,
                doc_hash, version, current_pages,
            )
            if chunk:
                chunks.append(chunk)
            current_text_parts = []
            current_block_ids = []
            current_tokens = 0
            current_pages = []

        # Accumulate
        current_text_parts.append(text)
        current_block_ids.append(block.block_id)
        current_tokens += text_tokens
        if page_num not in current_pages:
            current_pages.append(page_num)

        # Edge case: single block exceeds max_tokens on its own
        if current_tokens > max_tokens and len(current_text_parts) == 1:
            logger.warning(
                "Single block %s exceeds max_tokens (%d > %d), splitting",
                block.block_id, current_tokens, max_tokens,
            )
            chunks.extend(
                _split_text(
                    "\n".join(current_text_parts), max_tokens,
                    doc_hash, version, current_scope,
                    list(current_block_ids), list(current_pages),
                )
            )
            current_text_parts = []
            current_block_ids = []
            current_tokens = 0
            current_pages = []

    # Flush final chunk
    chunk = _flush_chunk(
        current_text_parts, current_scope, current_block_ids,
        doc_hash, version, current_pages,
    )
    if chunk:
        chunks.append(chunk)

    return chunks
