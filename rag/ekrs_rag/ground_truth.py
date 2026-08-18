"""Ground-truth label extraction heuristics for the recall@10 baseline.

Phase 12-A follow-up: per-(bundle, anchor) labels so the real-infra round
of ``scripts/recall_at_10_form_field_baseline.py`` measures true recall
instead of rank stability (script lines 430-438 in the pre-fix version).

Three pure functions, each takes a list of Qdrant-payload-shaped dicts
(matching ``Chunk.__init__`` defaults in
``shared/ekrs_shared/models.py:186-216`` + what
``EKRSRetriever._payload_to_chunk`` reads at ``retriever.py:243``) and
returns the matching ``chunk_id`` or ``None``.

Tie-break rules are aligned with the R4 scope-priority composite scoring
implemented in ``EKRSRetriever._scope_priority`` (retriever.py:269):
- Higher scope_priority wins (``scope_path[0]`` prefix lookup).
- Within same priority, lower ``chunk_index`` (parsed from
  ``{doc_hash[:8]}-{idx:04d}``) wins — earlier chunks hold more context.

The heading heuristic is **deferred** because ``heading_path`` is not
propagated to the Qdrant payload (see ``phase10-t10b2-closed.md``: doc-
to-md doesn't propagate outline.json heading_path into data.jsonl). It
always returns ``None`` and emits a module-level WARNING on first import
so the baseline script can SKIP heading anchors cleanly until the data-
quality gap is closed.

The module also exposes ``extract_anchors_for_bundle`` (orchestrator)
and ``build_sidecar`` (manifest-merging) + ``parse_lot_from_filename`` +
``first_column_header_value`` (anchor-value sources) — all pure,
importable by both the extractor script and the unit-test suite.
"""
from __future__ import annotations

import logging
import re
import warnings
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Scope priority constants — keep aligned with R4 in retriever.py
_SCOPE_PRIORITY_ORDER = (
    "national",
    "industry",
    "enterprise",
    "project",
    "reference",
)

_LOT_PATTERN = re.compile(r"\blot\s*0*(\d+)", re.IGNORECASE)


def _scope_priority_value(scope_path: List[str]) -> float:
    """Map scope_path[0] to R4 priority float; unknown scopes rank lowest.

    Mirrors the prefix lookup in EKRSRetriever._scope_priority.
    """
    if not scope_path:
        return 0.0
    head = scope_path[0].lower()
    try:
        idx = _SCOPE_PRIORITY_ORDER.index(head)
    except ValueError:
        return 0.0
    # national=1.0, industry=0.8, enterprise=0.6, project=0.4, reference=0.2
    return round(1.0 - idx * 0.2, 2)


def _chunk_index(chunk_id: Optional[str]) -> int:
    """Parse trailing 4-digit index from ``{doc_hash[:8]}-{idx:04d}`` chunk_id.

    Returns ``10**9`` for legacy chunks (None / no index suffix) so they
    sort LAST under ascending tie-break — preferring T10a-5+ chunks that
    actually went through the round-trip generator.
    """
    if not chunk_id:
        return 10**9
    m = re.search(r"-(\d{4})$", chunk_id)
    if not m:
        return 10**9
    return int(m.group(1))


def _pick_best(
    chunks: List[Dict[str, Any]],
    candidates: List[Dict[str, Any]],
) -> Optional[str]:
    """Tie-break across candidates by R4 scope_priority desc, chunk_index asc."""
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0].get("chunk_id")
    scored = sorted(
        candidates,
        key=lambda c: (
            -_scope_priority_value(c.get("scope_path", [])),
            _chunk_index(c.get("chunk_id")),
        ),
    )
    return scored[0].get("chunk_id")


def pick_form_field_chunk(
    chunks: List[Dict[str, Any]],
    lot_value: str,
) -> Optional[str]:
    """Return chunk_id of the chunk that contains ``lot_value`` AND has
    non-empty ``form_fields`` list.

    Heuristic:
      - Substring match ``lot_value`` in chunk's ``text`` field.
      - ``form_fields`` list is non-empty (the chunker populated it from
        ``block.metadata.form_fields``; empty list = legacy chunk or
        non-form-field block).
    Tie-break: R4 scope_priority desc, then chunk_index asc.
    """
    if not chunks or not lot_value:
        return None
    candidates = [
        c for c in chunks
        if lot_value in (c.get("text") or "")
        and (c.get("form_fields") or [])
    ]
    return _pick_best(chunks, candidates)


def pick_column_header_chunk(
    chunks: List[Dict[str, Any]],
    header_value: str,
) -> Optional[str]:
    """Return chunk_id of the chunk whose ``column_headers`` list contains
    a dict with ``name == header_value`` AND whose ``text`` substring-
    matches the header value.

    Heuristic rationale: column_headers is a list of dicts (Phase 12 T1
    schema) — we match on the dict's ``name`` field and require the
    header text to appear in the chunk's body text (otherwise the dict
    could be from a header-row-only block without body context).
    Tie-break: R4 scope_priority desc, then chunk_index asc.
    """
    if not chunks or not header_value:
        return None
    candidates = [
        c
        for c in chunks
        if header_value in (c.get("text") or "")
        and any(
            isinstance(h, dict) and h.get("name") == header_value
            for h in (c.get("column_headers") or [])
        )
    ]
    return _pick_best(chunks, candidates)


def pick_heading_chunk(
    chunks: List[Dict[str, Any]],  # noqa: ARG001 — kept for signature parity
    heading_value: str,  # noqa: ARG001
) -> Optional[str]:
    """DEFERRED — always returns None.

    ``heading_path`` is not propagated into the Qdrant payload (the
    data-quality issue documented in ``phase10-t10b2-closed.md``: doc-to-
    md doesn't propagate outline.json heading_path into data.jsonl). The
    heuristic cannot operate on the available payload fields without
    inventing a brittle text-matching rule.

    Returns ``None`` so the baseline script's SKIP path engages cleanly.
    Will be re-implemented when heading_path is added to the Chunk model
    and Qdrant payload (separate task — not in scope for 8/20 联调).
    """
    return None


# Emit one WARNING on first import so operators see the gap in logs even
# if no heading anchors are ever processed in a given run.
warnings.warn(
    "ekrs_rag.ground_truth.pick_heading_chunk is deferred: heading_path "
    "is not in Qdrant payload. Heading anchors will SKIP until the data-"
    "quality gap is closed (see phase10-t10b2-closed.md).",
    stacklevel=1,
)


# ---------------------------------------------------------------------------
# Public helpers (orchestrator + manifest merge + anchor-value sources)
# ---------------------------------------------------------------------------


def parse_lot_from_filename(filename: str) -> Optional[str]:
    """Extract ``Lot N`` from bundle filename, e.g. ``7-Lot00 NCR ...doc``
    → ``"Lot 0"`` (zero-padded numbers get normalized to ``"Lot 0"`` form).

    Falls back to ``None`` when no LOT pattern is present — the form_field
    anchor will then SKIP. Stripped leading zeros so ``"Lot 07"`` and
    ``"Lot 7"`` map to the same query.
    """
    m = _LOT_PATTERN.search(filename)
    if not m:
        return None
    return f"Lot {int(m.group(1))}"


def first_column_header_value(chunks: List[Dict[str, Any]]) -> Optional[str]:
    """Pick the first ``column_headers[].name`` we encounter across the
    bundle's chunks. Returns the header name string, or ``None`` if no
    chunk has a populated ``column_headers`` list.
    """
    for chunk in chunks:
        for h in chunk.get("column_headers") or []:
            if isinstance(h, dict) and h.get("name"):
                return h["name"]
    return None


def extract_anchors_for_bundle(
    bundle: Dict[str, Any],
    chunks: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Apply 3 heuristics and return ``ground_truth`` list for ONE bundle.

    Per-anchor SKIP + WARNING when the heuristic returns ``None``. The
    heading anchor is always SKIPPED in this version (heading_path is
    not in Qdrant payload — see ``phase10-t10b2-closed.md``).

    Returns a list of dicts with shape:
        ``{"anchor_type": str, "anchor_value": str, "expected_chunk_id": str}``
    """
    out: List[Dict[str, Any]] = []
    bundle_id = bundle.get("bundle_id", "<unknown>")

    # --- form_field ---
    lot_value = parse_lot_from_filename(bundle.get("filename", ""))
    if lot_value is None:
        logger.warning(
            "SKIP: %s/form_field — no LOT pattern in filename %r",
            bundle_id, bundle.get("filename"),
        )
    else:
        picked = pick_form_field_chunk(chunks, lot_value)
        if picked is None:
            logger.warning(
                "SKIP: %s/form_field — no candidate for lot_value=%r",
                bundle_id, lot_value,
            )
        else:
            out.append({
                "anchor_type": "form_field",
                "anchor_value": lot_value,
                "expected_chunk_id": picked,
            })

    # --- column_header ---
    header_value = first_column_header_value(chunks)
    if header_value is None:
        logger.warning(
            "SKIP: %s/column_header — no column_headers populated in any chunk",
            bundle_id,
        )
    else:
        picked = pick_column_header_chunk(chunks, header_value)
        if picked is None:
            logger.warning(
                "SKIP: %s/column_header — no candidate for header_value=%r",
                bundle_id, header_value,
            )
        else:
            out.append({
                "anchor_type": "column_header",
                "anchor_value": header_value,
                "expected_chunk_id": picked,
            })

    # --- heading (DEFERRED — always skip) ---
    picked = pick_heading_chunk(chunks, "")
    if picked is None:
        logger.warning(
            "SKIP: %s/heading — phase10-t10b2 heading_path gap (data quality)",
            bundle_id,
        )

    return out


def build_sidecar(
    bundles: List[Dict[str, Any]],
    ground_truth_by_bundle: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, Any]:
    """Build the sidecar JSON shape: original manifest metadata + annotated
    ``recommended_first`` array with new ``ground_truth`` field per entry.

    ``full_list`` is omitted from the sidecar (we only label the
    recommended-first subset). The original manifest is untouched.
    """
    labeled = []
    for b in bundles:
        new_entry = dict(b)
        new_entry["ground_truth"] = ground_truth_by_bundle.get(b["bundle_id"], [])
        labeled.append(new_entry)
    return {
        "summary": {
            "tool": "extract_ground_truth_labels.py",
            "phase": "12-A follow-up (8/20 联调 prep)",
            "label_format": {
                "anchor_type": "form_field | column_header | heading",
                "anchor_value": "string used as query (LOT or header name)",
                "expected_chunk_id": "Phase 12 T10a-5 chunk_id ({doc_hash[:8]}-{idx:04d})",
            },
            "per_anchor_policy": "SKIP+WARNING (no candidate) — not a failure",
        },
        "recommended_first": labeled,
    }


def anchors_from_sidecar(
    bundles: List[Dict[str, Any]],
    sidecar: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Flatten a sidecar manifest into a flat list of anchor dicts for
    the real-infra round of ``scripts/recall_at_10_form_field_baseline.py``.

    Per the 2026-08-18 lock-in ruling: per-anchor SKIP+WARNING (does NOT
    fail the batch). Each bundle's ``ground_truth`` list already encodes
    "skip" via empty list — heading anchors are simply absent (the
    extractor never emits them under the v1 heading_path gap).

    Per-bundle SKIP+WARNING when:
      - bundle_id not in sidecar's ``recommended_first`` (extractor never
        saw it, or manifest vs sidecar mismatch)
      - bundle's ``ground_truth`` list is empty (extractor found no
        candidates for any anchor type)

    Returns flat list of anchor dicts shaped for the baseline script's
    ``QueryAnchor`` conversion:
        ``{"bundle_id", "anchor_type", "anchor_value", "expected_chunk_id"}``
    """
    sidecar_by_id = {
        b["bundle_id"]: b.get("ground_truth", [])
        for b in (sidecar.get("recommended_first") or [])
    }
    anchors: List[Dict[str, Any]] = []
    for bundle in bundles:
        bundle_id = bundle["bundle_id"]
        gt = sidecar_by_id.get(bundle_id)
        if gt is None:
            logger.warning(
                "SKIP: %s — not in sidecar's recommended_first", bundle_id,
            )
            continue
        if not gt:
            logger.warning(
                "SKIP: %s — empty ground_truth (extractor found no candidates)",
                bundle_id,
            )
            continue
        for entry in gt:
            anchors.append({
                "bundle_id": bundle_id,
                "anchor_type": entry["anchor_type"],
                "anchor_value": entry["anchor_value"],
                "expected_chunk_id": entry["expected_chunk_id"],
            })
    return anchors


def filter_chunks_by_doc_prefix(
    chunks: List[Dict[str, Any]],
    prefix: str,
) -> List[Dict[str, Any]]:
    """Filter chunks whose ``doc_hash`` field starts with ``prefix``.

    Used by the extractor when the manifest's bundle_id is a prefix of
    the actual Qdrant doc_hash — the parser appends ``_r<timestamp>``
    suffix during ingestion-notify, so e.g. bundle ``abc123`` ends up
    stored as ``abc123_r20260728T045717Z``. The extractor fetches the
    full chunk payload (no Qdrant-side exact match available for the
    prefix), then filters in Python.

    Empty prefix is a degenerate case — ``str.startswith('')`` returns
    True for any string, so the filter becomes a pass-through. The
    extractor always passes a non-empty bundle_id, so this is safe in
    practice but documented for callers.
    """
    return [c for c in chunks if (c.get("doc_hash") or "").startswith(prefix)]