"""Semantic chunker — converts DocumentBlockIR list into Chunk objects.

Two-phase chunking (Phase 9):
  Phase 1 — _hard_cut: char-offset slicing with 20% look-back to a safe
            boundary when the cut would land mid-word. Prevents splitting
            number/unit pairs and English words across chunk boundaries.
  Phase 2 — _try_merge_fragments: greedy merge that respects token budget
            and refuses to join across an unsafe boundary (digit+letter,
            letter+digit, digit+'.', ASCII+ASCII letter, …).

Three boundary conditions (per design doc):
1. Scope change: heading_path differs → flush current chunk, start new
2. Table/kv type: standalone chunk, with header propagation on overflow
3. Token overflow: estimate via len/4, flush when exceeding max_tokens

Edge case: single block > max_tokens → split with warning log (not silent truncation).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Callable, Optional

from ekrs_shared.models import Chunk, DocumentBlockIR

from .ir_parser import extract_text

logger = logging.getLogger(__name__)


# Default chunk token budget. bge-m3 sweet spot is 512–1024; 768 yields
# ~3072 chars per chunk, reducing Qdrant point count ~35% vs the prior
# 500-token limit. Tunable via settings.MAX_CHUNK_TOKENS at call sites.
DEFAULT_MAX_CHUNK_TOKENS = 768

# Phase 1 look-back window: when a hard cut lands mid-word, look back up
# to this fraction of the cut distance for a safe boundary (space, punct,
# CJK transition). 0.2 = 20% — keeps chunk size variance bounded.
_LOOKBACK_RATIO = 0.2


def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token.

    Floor at 1 to mirror Phase 1 behavior so empty/whitespace fragments
    don't zero out budgets (callers explicitly filter empties).
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


# ============================================================================
# Phase 1: hard cut + look-back
# ============================================================================


def _is_safe_lookback_char(ch: str) -> bool:
    """A character that makes a cut position safe (no word would split).

    Safe characters:
    - whitespace (space, tab, newline)
    - CJK punctuation: 。 ！？ ； ， 、 etc.
    - ASCII punctuation that terminates a word: . , ; : ! ? ( ) [ ] { }
    - CJK ideographs themselves (transition between CJK chars is safe;
      cutting between two CJK chars never splits a word)
    """
    if ch.isspace():
        return True
    # ASCII punctuation that always terminates a word
    if ch in ".,;:!?()[]{}\"'-/\\|<>=+*&%@#$^_~`":
        return True
    # CJK punctuation range
    if "　" <= ch <= "〿":  # CJK symbols & punctuation
        return True
    if "＀" <= ch <= "￯":  # fullwidth forms
        return True
    # CJK Unified Ideographs (4E00–9FFF) — transitions between two CJK
    # ideographs never split a word, so the CJK→CJK boundary is safe.
    if "一" <= ch <= "鿿":
        return True
    # CJK Extension A
    if "㐀" <= ch <= "䶿":
        return True
    return False


def _find_safe_boundary(text: str, hard_cut_pos: int) -> int:
    """Look back from hard_cut_pos up to 20% of distance for a safe cut.

    When the trailing pattern at hard_cut_pos is a digit cluster (e.g.,
    "1.5倍", "350", "100MPa"), extends the look-back to find the start of
    the cluster — otherwise the cut would split the number from its unit.

    Returns the position AFTER the safe boundary char (the cut actually
    occurs at this position). If no safe boundary is found in the
    look-back window, returns hard_cut_pos unchanged (no infinite loop).
    """
    if hard_cut_pos <= 0 or hard_cut_pos >= len(text):
        return hard_cut_pos

    lookback = max(8, int(hard_cut_pos * _LOOKBACK_RATIO))
    window_start = max(0, hard_cut_pos - lookback)

    # Special handling for digit-cluster cuts: when hard cut lands inside
    # a numeric token like "1.5" or "350" or "100MPa", we must cut BEFORE
    # the cluster, not at the closest safe char (which would be the
    # decimal point '.' — splitting "1." from "5").
    if hard_cut_pos > 0:
        prev_char = text[hard_cut_pos - 1]
        # The cluster starts: scan backwards over digits and dots
        cluster_start = hard_cut_pos - 1
        while cluster_start > window_start:
            c = text[cluster_start]
            if c.isdigit() or c == ".":
                cluster_start -= 1
            else:
                break
        # If we found a digit/dot cluster, return position right after
        # the char BEFORE the cluster (i.e., cut before the number starts).
        # cluster_start currently points one past the last digit/dot, so
        # cluster_start+1 is the first digit/dot of the cluster.
        if cluster_start + 1 < hard_cut_pos and (
            text[cluster_start + 1].isdigit() or text[cluster_start + 1] == "."
        ):
            # Verify the char at cluster_start is a safe boundary
            if cluster_start >= 0 and _is_safe_lookback_char(text[cluster_start]):
                return cluster_start + 1

    # General case: scan backwards looking for a safe char.
    for pos in range(hard_cut_pos - 1, window_start - 1, -1):
        if _is_safe_lookback_char(text[pos]):
            return pos + 1

    # No safe boundary — return hard cut position unchanged.
    return hard_cut_pos


def _hard_cut(text: str, max_chars: int) -> list[str]:
    """Phase 1: char-offset slicing with 20% look-back to safe boundaries.

    Cuts text into pieces no longer than max_chars. When a cut would land
    in the middle of a word (ASCII letter on both sides of the cut), looks
    back up to 20% of the distance for a safe boundary (whitespace,
    punctuation, CJK transition). Falls back to the hard cut if no safe
    boundary exists in the window — guarantees progress.

    Returns a list of fragments. Empty/whitespace-only fragments are
    skipped. Rejoining the fragments always reconstructs the original
    text (no characters lost).
    """
    if not text:
        return []
    if max_chars <= 0:
        raise ValueError("max_chars must be > 0")
    if len(text) <= max_chars:
        return [text]

    fragments: list[str] = []
    start = 0
    n = len(text)

    while start < n:
        hard_end = min(start + max_chars, n)
        end = hard_end

        # Only attempt look-back when the cut is interior (not the last cut)
        # and we have a real cut boundary (not just n).
        if hard_end < n and hard_end > start + 1:
            left_char = text[hard_end - 1]
            right_char = text[hard_end]
            # Trigger look-back for any unsafe cut pattern:
            # - letter+letter: mid English word
            # - digit+digit: mid number (e.g., "350" → "35" + "0")
            # - digit+letter or letter+digit: mid number+unit
            # - digit+'.' or '.'+digit: mid decimal
            is_letter_cut = (
                left_char.isascii() and left_char.isalpha()
                and right_char.isascii() and right_char.isalpha()
            )
            is_digit_cut = left_char.isdigit() and right_char.isdigit()
            is_digit_letter_cut = (
                left_char.isdigit() and right_char.isascii() and right_char.isalpha()
            ) or (
                left_char.isascii() and left_char.isalpha() and right_char.isdigit()
            )
            is_decimal_cut = (
                (left_char.isdigit() and right_char == ".")
                or (left_char == "." and right_char.isdigit())
            )
            if is_letter_cut or is_digit_cut or is_digit_letter_cut or is_decimal_cut:
                safe_end = _find_safe_boundary(text, hard_end)
                if safe_end > start:
                    end = safe_end

        fragment = text[start:end]
        if fragment.strip():
            fragments.append(fragment)
        start = end

    return fragments


# ============================================================================
# Phase 2: safe-join check + greedy merge
# ============================================================================


def _is_safe_join_boundary(left: str, right: str) -> bool:
    """Decide whether two adjacent fragments can be safely joined.

    Returns False when joining would form a boundary that splits a
    semantic unit:
    - digit + ASCII letter: e.g., "100" + "MPa" — the boundary was cut
      inside a number+unit pair (numeric_hint_extractor needs them together).
    - ASCII letter + digit: e.g., "MPa" + "100".
    - digit + '.' or '.' + digit: decimal mid-split, e.g., "3" + ".14".
    - ASCII letter + ASCII letter: English word mid-split, e.g., "pres" + "sure".

    Returns True for empty boundaries, CJK transitions, punctuation
    transitions, whitespace transitions, and digit+CJK-unit transitions
    (e.g., "350" + "℃" — merging yields the natural "350℃" which is
    exactly what we want; CJK units are not ASCII letters so they don't
    trigger the number+unit refusal).

    Note: Python's str.isalpha() returns True for CJK ideographs, so we
    explicitly check isascii() before treating a char as an ASCII letter.
    """
    if not left or not right:
        return True

    l_char = left[-1]
    r_char = right[0]

    # Number+ASCII-letter unit: refuse to merge across this boundary
    # because the cut separated a number from its ASCII-letter unit
    # (e.g., "100" | "MPa" → keeping them apart preserves atomicity).
    if l_char.isdigit() and r_char.isascii() and r_char.isalpha():
        return False
    # ASCII letter + number: same logic, opposite direction.
    if l_char.isascii() and l_char.isalpha() and r_char.isdigit():
        return False
    # Decimal mid-split.
    if l_char.isdigit() and r_char == ".":
        return False
    if l_char == "." and r_char.isdigit():
        return False
    # ASCII letter + ASCII letter: English word mid-split.
    if (
        l_char.isascii() and l_char.isalpha()
        and r_char.isascii() and r_char.isalpha()
    ):
        return False

    # Everything else is safe: CJK-to-CJK, CJK-to-ASCII (including CJK
    # units like ℃/度), punctuation, whitespace, digit+CJK unit, etc.
    return True


def _try_merge_fragments(
    fragments: list[str],
    max_tokens: int,
    token_counter: Callable[[str], int],
) -> list[str]:
    """Phase 2: greedy merge respecting token budget and safe boundaries.

    Iterates fragments in order, accumulating into the current chunk as long
    as (a) joining the new fragment at the boundary is safe per
    _is_safe_join_boundary, and (b) the resulting chunk stays within
    max_tokens. Otherwise flushes the current chunk and starts a new one.

    Empty fragments are skipped. Single-fragment input returns a single-
    element list. Empty input returns [].
    """
    if not fragments:
        return []
    if max_tokens <= 0:
        raise ValueError("max_tokens must be > 0")

    chunks: list[str] = []
    current: list[str] = []
    current_tokens = 0

    for frag in fragments:
        if not frag or not frag.strip():
            continue

        frag_tokens = token_counter(frag)

        if not current:
            # First non-empty fragment: always starts a new chunk.
            current = [frag]
            current_tokens = frag_tokens
            continue

        # Check both conditions: safe boundary AND budget remaining.
        if (
            _is_safe_join_boundary(current[-1], frag)
            and current_tokens + frag_tokens <= max_tokens
        ):
            current.append(frag)
            current_tokens += frag_tokens
        else:
            # Flush current chunk and start a new one with this fragment.
            chunks.append("".join(current))
            current = [frag]
            current_tokens = frag_tokens

    if current:
        chunks.append("".join(current))

    return chunks


def split_text_by_tokens(
    text: str,
    max_tokens: int = DEFAULT_MAX_CHUNK_TOKENS,
    token_counter: Callable[[str], int] = estimate_tokens,
    hard_cut_chars: Optional[int] = None,
) -> list[str]:
    """Convenience entry point: Phase 1 hard cut → Phase 2 greedy merge.

    Returns a list of chunk strings. ``hard_cut_chars`` defaults to
    ``max_tokens * 4`` (the estimate_tokens inverse), which matches the
    runtime semantic. Tests may override either parameter to exercise
    edge cases without needing a real tokenizer.
    """
    if not text:
        return []
    if hard_cut_chars is None:
        hard_cut_chars = max_tokens * 4

    fragments = _hard_cut(text, hard_cut_chars)
    return _try_merge_fragments(fragments, max_tokens, token_counter)


# ============================================================================
# Atomicity validator (used by golden tests)
# ============================================================================


def validate_chunk_atomicity(chunk: str) -> bool:
    """Validate that a chunk preserves number/unit and word atomicity.

    Returns False for atomicity-breaking patterns:
    - empty or whitespace-only chunk
    - chunk ending with an ASCII digit whose immediately-following char
      (in the same chunk) is not a CJK unit character (℃/度/倍 etc.).
      This pattern indicates the chunk was hard-cut between a number
      and its unit/suffix, leaving numeric_hint_extractor unable to
      pair the digits with their semantic unit.

    Returns True for chunks that don't exhibit the bare-digit-ending
    pattern. Cross-chunk safety (no unsafe boundary between adjacent
    chunks) is verified separately via _is_safe_join_boundary — that
    is the authoritative gate for atomicity.
    """
    if not chunk:
        return False
    stripped = chunk.rstrip()
    if not stripped:
        return False
    if stripped[-1].isdigit():
        # Walk back to find the digit cluster, then check what follows.
        # If the chunk ends with "350" with no unit in the same chunk,
        # the unit must live in the next chunk → atomicity broken.
        # (Note: this is a heuristic; the authoritative check is
        # _is_safe_join_boundary on adjacent pairs.)
        return False
    return True


# ============================================================================
# Legacy chunking (preserved paths)
# ============================================================================


def _flush_chunk(
    text_parts: list[str],
    scope_path: list[str],
    block_ids: list[str],
    doc_hash: str,
    version: int,
    page_numbers: list[int],
    payload_version: int = 1,
    form_fields: Optional[list[dict[str, Any]]] = None,
    column_headers: Optional[list[dict[str, Any]]] = None,
    doc_type: Optional[str] = None,
) -> Optional[Chunk]:
    """Build a Chunk from accumulated text parts. Returns None if empty.

    Phase 12 T2: form_fields / column_headers carried from accumulator
    (set by chunk_blocks when scanning the leading block) to the chunk.
    Phase 12 Task C: doc_type stamped onto every produced Chunk (None = legacy).
    """
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
        payload_version=payload_version,
        form_fields=list(form_fields) if form_fields else [],
        column_headers=list(column_headers) if column_headers else [],
        doc_type=doc_type,
    )


def _route_accumulated_group(
    text_parts: list[str],
    scope_path: list[str],
    block_ids: list[str],
    doc_hash: str,
    version: int,
    page_numbers: list[int],
    max_tokens: int,
    token_counter: Callable[[str], int],
    payload_version: int = 1,
    form_fields: Optional[list[dict[str, Any]]] = None,
    column_headers: Optional[list[dict[str, Any]]] = None,
    doc_type: Optional[str] = None,
) -> list[Chunk]:
    """Route accumulated text_parts to _flush_chunk or _split_text_two_phase.

    Boundary 2 (scope-change) and Boundary 3 (token-overflow) use this helper
    instead of calling _flush_chunk directly. Decision order:

    1. Any unsafe adjacent pair (per _is_safe_join_boundary) → _split_text_two_phase
       (even if token count is within budget; defends against future seams
       removal that would re-introduce mid-atom splits).
    2. token_counter("\\n".join(text_parts)) > max_tokens → _split_text_two_phase
       (replaces the pre-T10b-1 "force-merge into one oversized chunk" behavior).
    3. Default → _flush_chunk (whole-group merge to 1 chunk; preserves the
       pre-T10b-1 happy-path behavior for groups that fit).

    Replaces the prior "_flush_chunk with conditional route" inline code at
    chunk_blocks:676 (Boundary 2) and chunk_blocks:690 (Boundary 3). Both
    sites share this helper so the two boundaries stay synchronized (T10b-1
    finding: deep nesting triggers token-overflow more often than scope-change).

    Phase 12 T2: form_fields / column_headers threaded through both paths.
    Phase 12 Task C: doc_type threaded through both paths.
    """
    # Guard 1: unsafe adjacent pair → safe-split path
    for prev, nxt in zip(text_parts[:-1], text_parts[1:]):
        if not _is_safe_join_boundary(prev, nxt):
            return _split_text_two_phase(
                "\n".join(text_parts), max_tokens,
                doc_hash, version, scope_path, block_ids,
                page_numbers, payload_version,
                form_fields=form_fields,
                column_headers=column_headers,
                doc_type=doc_type,
            )

    # Guard 2: token budget overflow → safe-split path
    full_text = "\n".join(text_parts)
    if token_counter(full_text) > max_tokens:
        return _split_text_two_phase(
            full_text, max_tokens,
            doc_hash, version, scope_path, block_ids,
            page_numbers, payload_version,
            form_fields=form_fields,
            column_headers=column_headers,
            doc_type=doc_type,
        )

    # Default: whole-group merge (preserves pre-T10b-1 behavior)
    chunk = _flush_chunk(
        text_parts, scope_path, block_ids,
        doc_hash, version, page_numbers, payload_version,
        form_fields=form_fields,
        column_headers=column_headers,
        doc_type=doc_type,
    )
    return [chunk] if chunk else []


def _build_chunk(
    text: str,
    scope_path: list[str],
    block_id: str,
    doc_hash: str,
    version: int,
    page_number: int,
    payload_version: int = 1,
    form_fields: Optional[list[dict[str, Any]]] = None,
    column_headers: Optional[list[dict[str, Any]]] = None,
    doc_type: Optional[str] = None,
) -> Chunk:
    """Build a single Chunk from a split fragment.

    Phase 12 T2: form_fields / column_headers carried from block to chunk
    for R4 scope-aware boost (parent §三 form_field_r4 plan).
    Phase 12 Task C: doc_type stamped onto the chunk (None = legacy).
    """
    return Chunk(
        text=text,
        scope_path=list(scope_path),
        source_block_ids=[block_id],
        token_count=estimate_tokens(text),
        doc_hash=doc_hash,
        version=version,
        page_numbers=[page_number],
        payload_version=payload_version,
        form_fields=list(form_fields) if form_fields else [],
        column_headers=list(column_headers) if column_headers else [],
        doc_type=doc_type,
    )


def _split_large_block(
    block: DocumentBlockIR,
    text: str,
    max_tokens: int,
    doc_hash: str,
    version: int,
    scope_path: list[str],
    page_numbers: list[int],
    payload_version: int = 1,
    form_fields: Optional[list[dict[str, Any]]] = None,
    column_headers: Optional[list[dict[str, Any]]] = None,
    doc_type: Optional[str] = None,
) -> list[Chunk]:
    """Split a single large block that exceeds max_tokens.

    For tables: propagate column headers to each sub-chunk (preserves the
    header-row redundancy pattern; unchanged from prior implementation).
    For text: use Phase 1 + Phase 2 to preserve number/unit/word atomicity.

    Phase 12 Task C: doc_type stamped onto every produced Chunk (None = legacy).

    Phase 12 row-flush fix: guarantees every output chunk has
    ``token_count <= max_tokens``. Two protections:
      1. Pre-check: if a single row's tokens exceed max_tokens, flush the
         current buffer first and force-split the oversized row via
         ``_split_text_two_phase`` (each sub-chunk then ≤ max_tokens).
         Force-split chunks carry ``quality_warning=True`` (source-quality
         hint for retriever; data is still indexed).
      2. Post-flush re-check: any accumulated buffer that, after joining,
         still exceeds max_tokens (e.g. header + one near-cap row) is
         force-split via ``_split_text_two_phase`` instead of emitted as a
         single oversized chunk. This eliminates the historical wedge
         (267 pathological rows × ~1200 tokens producing 1500-2200 token
         chunks that wedge bge-m3 encoding past the 600s status timeout).
    """
    chunks: list[Chunk] = []

    def _emit_buffer(parts: list[str], *, quality_warning: bool = False) -> None:
        """Flush ``parts`` as one or more Chunks, force-splitting if joined text exceeds max_tokens."""
        if not parts:
            return
        joined = "\n".join(p for p in parts if p).strip()
        if not joined:
            return
        joined_tokens = estimate_tokens(joined)
        if joined_tokens <= max_tokens:
            chunks.append(Chunk(
                text=joined,
                scope_path=list(scope_path),
                source_block_ids=[block.block_id],
                token_count=joined_tokens,
                doc_hash=doc_hash,
                version=version,
                page_numbers=list(page_numbers),
                payload_version=payload_version,
                form_fields=list(form_fields) if form_fields else [],
                column_headers=list(column_headers) if column_headers else [],
                doc_type=doc_type,
                quality_warning=quality_warning,
            ))
            return
        # Buffer oversize (e.g. header + one near-cap row combined). Force-split
        # via two-phase: each produced chunk will be ≤ max_tokens.
        # Mark the just-appended sub-chunks with quality_warning.
        pre_count = len(chunks)
        chunks.extend(_split_text_two_phase(
            joined, max_tokens, doc_hash, version, scope_path,
            [block.block_id], page_numbers, payload_version,
            form_fields=form_fields, column_headers=column_headers,
            doc_type=doc_type,
        ))
        if quality_warning:
            for c in chunks[pre_count:]:
                c.quality_warning = True

    if block.type == "table":
        headers = extract_table_headers(block)
        header_prefix = " | ".join(headers) + "\n" if headers else ""

        # Split by rows if structured data available
        if block.content.structured and isinstance(block.content.structured, list):
            rows = block.content.structured
            header_row = rows[0] if rows else []
            data_rows = rows[1:] if rows else []

            current_parts: list[str] = [header_prefix] if header_prefix else []
            current_tokens = estimate_tokens(header_prefix) if header_prefix else 0
            # Phase 12 row-flush fix #3 (post-closure): track raw chars so the
            # flush trigger matches ``_emit_buffer``'s exact joined-text size
            # check. ``estimate_tokens`` rounds down per row, losing ~0.25
            # tokens/row to integer division — for a 100-row buffer that's
            # ~25 tokens of phantom overhead that pushes the joined text over
            # max_tokens and triggers false-positive force-split. Using
            # current_parts_chars directly avoids this drift.
            current_parts_chars = len(header_prefix) if header_prefix else 0

            for row in data_rows:
                row_text = " | ".join(str(c) for c in row)
                row_tokens = estimate_tokens(row_text)

                # Phase 12 row-flush fix #1: pre-check oversized row.
                if row_tokens > max_tokens:
                    # Flush current buffer first (will be re-checked by _emit_buffer).
                    if current_parts:
                        _emit_buffer(current_parts)
                        current_parts = [header_prefix] if header_prefix else []
                        current_tokens = estimate_tokens(header_prefix) if header_prefix else 0
                    # Force-split the oversized row alone via _split_text_two_phase
                    # so sub-chunks each ≤ max_tokens. The header is NOT prepended:
                    # the row's pathological content is the source-quality issue,
                    # and prepending the header would force-split the header into
                    # a separate sub-chunk and propagate quality_warning to it
                    # (false-positive on a non-pathological metadata fragment).
                    # Header context is preserved implicitly via scope_path and
                    # column_headers fields already on every chunk.
                    pre_count = len(chunks)
                    chunks.extend(_split_text_two_phase(
                        row_text, max_tokens, doc_hash, version, scope_path,
                        [block.block_id], page_numbers, payload_version,
                        form_fields=form_fields, column_headers=column_headers,
                        doc_type=doc_type,
                    ))
                    # Mark the just-appended sub-chunks with quality_warning.
                    for c in chunks[pre_count:]:
                        c.quality_warning = True
                    continue  # Skip normal append+flush logic below.

                # Original flush-on-overflow logic (preserved), but routes
                # through _emit_buffer so post-flush re-check #2 kicks in.
                # Phase 12 row-flush fix #3 (Post-closure incremental):
                # Track ``current_parts_chars`` to match ``_emit_buffer``'s
                # exact ``len(joined) // 4`` computation in the joined-text
                # force-split guard. Using ``current_tokens + row_tokens``
                # alone underestimates because ``estimate_tokens`` rounds
                # down per row (e.g. 29 chars → 7 tokens; true value 7.25),
                # so the per-row accumulation loses ~0.25 tokens/row to
                # integer-division truncation, plus "\n" separators add
                # ~0.25 tokens/row. For a 100-row buffer that's ~50 tokens
                # of "phantom" overhead that pushes the joined text over
                # max_tokens and triggers force-split, which calls
                # ``_split_text_two_phase`` on line-bounded row text and
                # produces one chunk per row (3869 chunks for a 3860-row
                # 危险品目录 block instead of ~36). Track raw chars so the
                # flush trigger and the emit guard agree.
                projected_joined_chars = current_parts_chars + len(row_text) + 1
                if (
                    projected_joined_chars > max_tokens * 4
                    and len(current_parts) > (1 if header_prefix else 0)
                ):
                    _emit_buffer(current_parts)
                    current_parts = [header_prefix] if header_prefix else []
                    current_parts_chars = len(header_prefix) if header_prefix else 0
                    current_tokens = estimate_tokens(header_prefix) if header_prefix else 0

                current_parts.append(row_text)
                current_parts_chars += len(row_text) + 1  # +1 for "\n" join separator
                current_tokens += row_tokens

            # Flush remaining (uses _emit_buffer; re-check protects against
            # oversized final buffer when header + last near-cap row combined).
            # Guard `len > 1 if header else > 0` prevents emitting a phantom
            # header-only chunk when the last row was force-split (pre-check
            # leaves `current_parts = [header_prefix]` with no data rows).
            if current_parts and len(current_parts) > (1 if header_prefix else 0):
                _emit_buffer(current_parts)
        else:
            # No structured data: fall through to two-phase text splitting.
            chunks.extend(
                _split_text_two_phase(
                    text, max_tokens, doc_hash, version, scope_path,
                    [block.block_id], page_numbers, payload_version,
                    form_fields=form_fields,
                    column_headers=column_headers,
                    doc_type=doc_type,
                )
            )
    else:
        chunks.extend(
            _split_text_two_phase(
                text, max_tokens, doc_hash, version, scope_path,
                [block.block_id], page_numbers, payload_version,
                form_fields=form_fields,
                column_headers=column_headers,
                doc_type=doc_type,
            )
        )

    if not chunks:
        logger.warning(
            "Block %s produced no chunks after split (text length=%d)",
            block.block_id, len(text),
        )

    return chunks


def _split_text_two_phase(
    text: str,
    max_tokens: int,
    doc_hash: str,
    version: int,
    scope_path: list[str],
    block_ids: list[str],
    page_numbers: list[int],
    payload_version: int = 1,
    form_fields: Optional[list[dict[str, Any]]] = None,
    column_headers: Optional[list[dict[str, Any]]] = None,
    doc_type: Optional[str] = None,
) -> list[Chunk]:
    """Split text via Phase 1 hard cut + Phase 2 greedy merge.

    Replaces the legacy _split_text pure-char-offset approach (which
    broke semantic atomicity). Public callers (chunk_blocks, _split_large_block)
    route through this entry point for both the single-block overflow
    edge case and the large-block table-text fallback.

    Phase 12 T2: form_fields / column_headers propagated to all sub-chunks
    so multi-block grouping retains the leading block's form metadata.
    Phase 12 Task C: doc_type stamped onto every produced Chunk (None = legacy).
    """
    chunks: list[Chunk] = []
    # Split on newlines first — line boundaries are always safe.
    # Per-line hard cut + merge keeps each chunk ≤ max_tokens.
    for line in text.split("\n"):
        if not line.strip():
            continue
        # Phase 1+2: hard cut → greedy merge
        fragments = _hard_cut(line, max_chars=max_tokens * 4)
        chunk_texts = _try_merge_fragments(
            fragments, max_tokens=max_tokens, token_counter=estimate_tokens,
        )
        for ct in chunk_texts:
            chunks.append(Chunk(
                text=ct,
                scope_path=list(scope_path),
                source_block_ids=list(block_ids),
                token_count=estimate_tokens(ct),
                doc_hash=doc_hash,
                version=version,
                page_numbers=list(page_numbers),
                payload_version=payload_version,
                form_fields=list(form_fields) if form_fields else [],
                column_headers=list(column_headers) if column_headers else [],
                doc_type=doc_type,
            ))

    return chunks


def chunk_blocks(
    blocks: list[DocumentBlockIR],
    doc_hash: str,
    version: int,
    max_tokens: int = DEFAULT_MAX_CHUNK_TOKENS,
    token_counter: Callable[[str], int] = estimate_tokens,
    payload_version: int = 1,
    doc_type: Optional[str] = None,
) -> list[Chunk]:
    """Convert DocumentBlockIR list into Chunk objects.

    Semantic chunking with three boundary conditions:
    1. Scope change (heading_path differs) — routed via ``_route_accumulated_group``
       (T10b-1): unsafe adjacent pairs OR token-budget overflow split via
       ``_split_text_two_phase``; otherwise the group merges to a single chunk.
    2. Table/kv → standalone chunk
    3. Token overflow → flush and start new — also routed via
       ``_route_accumulated_group`` (T10b-1), synchronized with Boundary 2.

    Within each chunk, two-phase splitting preserves number/unit and
    English word atomicity (Phase 1 hard cut + 20% look-back, Phase 2
    greedy merge with safe-boundary check).

    The ``token_counter`` parameter is wired through to Phase 2 merge
    decisions. The default (estimate_tokens) matches runtime; tests can
    override to a raw ``len`` counter to exercise budget semantics
    directly. The ``payload_version`` parameter lets callers force a
    Qdrant rebuild without bumping the on-disk document version.

    Phase 12 Task C: ``doc_type`` (default None = legacy) is stamped onto
    every produced Chunk. Set by the caller from
    ``rag.doc_classifier.classify()`` (one of national_standard /
    industry_standard / enterprise_spec / lot_checklist / project_spec /
    unknown). Legacy call sites that omit ``doc_type`` keep byte-level
    behavior (chunks carry ``doc_type=None``).

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
    # Phase 12 T2: form_fields / column_headers propagated across multi-block
    # groups so R4 boost applies to merged text chunks. Tracks the leading
    # block's metadata (subsequent blocks don't reset unless scope changes).
    current_form_fields: list[dict[str, Any]] = []
    current_column_headers: list[dict[str, Any]] = []

    for block in blocks:
        text = extract_text(block)
        if not text:
            continue

        scope = _get_scope_path(block)
        page_num = block.metadata.page_number
        block_form_fields = list(block.metadata.form_fields) if block.metadata.form_fields else []
        block_column_headers = list(block.metadata.column_headers) if block.metadata.column_headers else []

        # Boundary 1: Table/kv → standalone chunk
        if block.type in ("table", "kv"):
            # Flush accumulated text chunk first
            chunk = _flush_chunk(
                current_text_parts, current_scope, current_block_ids,
                doc_hash, version, current_pages, payload_version,
                form_fields=current_form_fields,
                column_headers=current_column_headers,
                doc_type=doc_type,
            )
            if chunk:
                chunks.append(chunk)
                current_text_parts = []
                current_block_ids = []
                current_tokens = 0
                current_pages = []
                current_form_fields = []
                current_column_headers = []

            # Create standalone chunk for table/kv
            block_tokens = estimate_tokens(text)
            if block_tokens > max_tokens:
                # Large block: split with header propagation
                logger.info(
                    "Large %s block %s (%d tokens), splitting",
                    block.type, block.block_id, block_tokens,
                )
                chunks.extend(
                    _split_large_block(
                        block, text, max_tokens, doc_hash, version, scope,
                        [page_num], payload_version,
                        form_fields=block_form_fields,
                        column_headers=block_column_headers,
                        doc_type=doc_type,
                    )
                )
            else:
                chunks.append(_build_chunk(
                    text, scope, block.block_id, doc_hash, version, page_num,
                    payload_version,
                    form_fields=block_form_fields,
                    column_headers=block_column_headers,
                    doc_type=doc_type,
                ))

            current_scope = scope
            continue

        # Boundary 2: Scope change → route accumulated group
        # T10b-1: replaces pre-fix _flush_chunk with _route_accumulated_group
        # so unsafe adjacent pairs OR token overflow route to _split_text_two_phase
        # instead of force-merging into one oversized chunk.
        if scope != current_scope:
            if current_text_parts:
                chunks.extend(_route_accumulated_group(
                    current_text_parts, current_scope, current_block_ids,
                    doc_hash, version, current_pages,
                    max_tokens, token_counter, payload_version,
                    form_fields=current_form_fields,
                    column_headers=current_column_headers,
                    doc_type=doc_type,
                ))
            current_text_parts = []
            current_block_ids = []
            current_tokens = 0
            current_pages = []
            current_form_fields = []
            current_column_headers = []
            current_scope = scope

        # Boundary 3: Token overflow → route accumulated group
        # T10b-1: synchronized with Boundary 2 to keep the two "force-merge"
        # edges consistent. Deep-nesting docs trigger token-overflow more often
        # than scope-change (Phase 10 plan §GSTACK REVIEW [HIGH] finding).
        text_tokens = estimate_tokens(text)
        if current_tokens + text_tokens > max_tokens and current_text_parts:
            chunks.extend(_route_accumulated_group(
                current_text_parts, current_scope, current_block_ids,
                doc_hash, version, current_pages,
                max_tokens, token_counter, payload_version,
                form_fields=current_form_fields,
                column_headers=current_column_headers,
                doc_type=doc_type,
            ))
            current_text_parts = []
            current_block_ids = []
            current_tokens = 0
            current_pages = []
            current_form_fields = []
            current_column_headers = []

        # Accumulate
        # Phase 12 T2: first block in a group seeds the form_fields /
        # column_headers; subsequent blocks in the same scope group do NOT
        # reset them. Rationale: form metadata is document/page-level, not
        # per-block — a multi-block "Checklist" group should retain the
        # original page's form fields across all merged text chunks.
        if not current_form_fields:
            current_form_fields = block_form_fields
        if not current_column_headers:
            current_column_headers = block_column_headers
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
                _split_text_two_phase(
                    "\n".join(current_text_parts), max_tokens,
                    doc_hash, version, current_scope,
                    list(current_block_ids), list(current_pages),
                    payload_version,
                    form_fields=current_form_fields,
                    column_headers=current_column_headers,
                    doc_type=doc_type,
                )
            )
            current_text_parts = []
            current_block_ids = []
            current_tokens = 0
            current_pages = []
            current_form_fields = []
            current_column_headers = []

    # Flush final chunk
    chunk = _flush_chunk(
        current_text_parts, current_scope, current_block_ids,
        doc_hash, version, current_pages, payload_version,
        form_fields=current_form_fields,
        column_headers=current_column_headers,
        doc_type=doc_type,
    )
    if chunk:
        chunks.append(chunk)

    return chunks