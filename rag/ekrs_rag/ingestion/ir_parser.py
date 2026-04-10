"""DocumentBlock IR parser — reads JSONL lines from doc-to-md output.

Each line in data.jsonl is a DocumentBlockIR object.
This module validates the line and extracts text + metadata for chunking.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ekrs_shared.models import DocumentBlockIR

logger = logging.getLogger(__name__)


class IRParseError(Exception):
    """Raised when a JSONL line fails validation."""


def parse_document_block(line: str) -> DocumentBlockIR:
    """Parse and validate a single JSONL line as DocumentBlockIR.

    Raises IRParseError on invalid JSON or missing required fields.
    """
    line = line.strip()
    if not line:
        raise IRParseError("Empty line")

    try:
        data = json.loads(line)
    except json.JSONDecodeError as e:
        raise IRParseError(f"Invalid JSON: {e}") from e

    # Validate required fields exist
    for field in ("doc_id", "block_id", "type"):
        if field not in data:
            raise IRParseError(f"Missing required field: {field}")

    # Ensure content has md_preview or raw
    content = data.get("content", {})
    if not isinstance(content, dict):
        content = {}
        data["content"] = content

    # Default md_preview to empty string if missing
    content.setdefault("md_preview", "")
    content.setdefault("raw", "")

    try:
        return DocumentBlockIR(**data)
    except Exception as e:
        raise IRParseError(f"Schema validation failed: {e}") from e


def extract_text(block: DocumentBlockIR) -> str:
    """Extract displayable text from a DocumentBlock.

    Priority: content.md_preview → content.raw → empty string.
    """
    if block.content.md_preview:
        return block.content.md_preview
    return block.content.raw


def extract_metadata(block: DocumentBlockIR) -> dict[str, Any]:
    """Extract metadata dict from a DocumentBlock for chunk construction."""
    return {
        "page_number": block.metadata.page_number,
        "heading_path": block.metadata.heading_path or [],
        "block_id": block.block_id,
        "type": block.type,
        "doc_id": block.doc_id,
    }


def parse_jsonl_file(file_path: str) -> list[DocumentBlockIR]:
    """Read an entire JSONL file and return parsed DocumentBlockIR list.

    Raises IRParseError on first invalid line (fail-fast, per design doc).
    """
    blocks: list[DocumentBlockIR] = []

    with open(file_path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                blocks.append(parse_document_block(line))
            except IRParseError as e:
                raise IRParseError(f"Line {line_num}: {e}") from e

    if not blocks:
        logger.warning("JSONL file is empty: %s", file_path)

    return blocks
