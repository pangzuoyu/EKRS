"""T10b-1 chunker-level stress: 60 realistic-shaped docs through chunk_blocks.

Direct call to the new chunker code (no docker/HTTP path). Validates that
the Boundary-2 + Boundary-3 routing change does not regress on
real-feeling corpus text — same shape as scripts/live_stress_60.py's
synthetic fallback.

Phase 10 T10b-1 Task 4 (Ta.4 stress). Companion to the 10k heavy bench
(which tests throughput); this one tests semantic correctness on
multi-chunk docs that exercise the new helper.

Exit codes:
  0 — pass (all 60 docs chunk cleanly, no oversized chunk beyond budget)
  1 — failure (oversized chunk detected, OR exceptions during chunking)
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# repo layout: rag/ekrs_rag/ingestion/chunker.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "rag"))

from ekrs_rag.ingestion.chunker import chunk_blocks  # noqa: E402
from ekrs_shared.models import (  # noqa: E402
    Content,
    DocumentBlockIR,
    Lineage,
    Metadata,
)


def _block(text: str, doc_id: str, bid: str, scope: list[str], page: int = 1) -> DocumentBlockIR:
    return DocumentBlockIR(
        doc_id=doc_id,
        block_id=bid,
        type="text",
        content=Content(raw=text, md_preview=text),
        metadata=Metadata(page_number=page, heading_path=list(scope)),
        lineage=Lineage(parser_version="stress", strategy="stress", steps=[]),
        uncertainty_score=0.0,
    )


def build_doc(doc_id: str) -> list[DocumentBlockIR]:
    """Build a 20-block doc with 4 scope-changes + 1 token-overflow trigger.

    Mirrors scripts/live_stress_60.py single-block doc shape but with
    explicit scope variation so Boundary 2 (and likely Boundary 3) get
    exercised under T10b-1.
    """
    blocks: list[DocumentBlockIR] = []
    # Section 1: 压力容器设计 (5 blocks)
    for i in range(5):
        text = (
            f"压力容器应符合GB 150标准，shell设计采用SA-516 Grade 70钢材，"
            f"屈服强度260 MPa以上。检验项目{i}应当记录在案。"
        )
        blocks.append(_block(text, doc_id, f"{doc_id}_b{i:02d}", ["第3章 压力容器"]))
    # Section 2: 混凝土养护 (5 blocks)
    for i in range(5, 10):
        text = (
            f"混凝土养护温度不得超过80°C，养护时间不少于7天。"
            f"关键工艺参数应当记录编号{i}。"
        )
        blocks.append(_block(text, doc_id, f"{doc_id}_b{i:02d}", ["第4章 混凝土工程"]))
    # Section 3: long single block to trigger Boundary 3 token-overflow
    long_text = (
        "预应力张拉控制应力为fptk=1860MPa，张拉力为195kN。"
        "锚具采用OVM型，混凝土强度等级不低于C40。"
    ) * 30  # ~1800 chars → ~450 tokens, exceeds 200-token budget
    blocks.append(_block(long_text, doc_id, f"{doc_id}_b10", ["第5章 预应力施工"]))
    # Section 4: 5 more small blocks
    for i in range(11, 16):
        text = f"第5章 第{i-10}节：施工要点应当符合规范要求。"
        blocks.append(_block(text, doc_id, f"{doc_id}_b{i:02d}", ["第5章 预应力施工"]))
    # Section 5: 4 more small blocks (final scope change)
    for i in range(16, 20):
        text = f"第6章 第{i-15}节：验收标准应当经监理单位确认。"
        blocks.append(_block(text, doc_id, f"{doc_id}_b{i:02d}", ["第6章 验收"]))
    return blocks


def main() -> int:
    n = 60
    max_tokens = 200
    budget_violations: list[tuple[str, int, int]] = []
    total_chunks = 0
    failures: list[tuple[str, str]] = []
    per_doc_chunks: list[int] = []

    t0 = time.perf_counter()
    for i in range(n):
        doc_id = f"stress_doc_{i:03d}"
        try:
            blocks = build_doc(doc_id)
            chunks = chunk_blocks(
                blocks,
                doc_hash=f"hash_{i}",
                version=1,
                max_tokens=max_tokens,
            )
        except Exception as e:
            failures.append((doc_id, f"{type(e).__name__}: {e}"))
            continue

        per_doc_chunks.append(len(chunks))
        total_chunks += len(chunks)
        for j, ch in enumerate(chunks):
            tk = ch.token_count or 0
            if tk > max_tokens:
                budget_violations.append((doc_id, j, tk))

    elapsed = time.perf_counter() - t0
    avg = sum(per_doc_chunks) / max(len(per_doc_chunks), 1)
    print(f"[T10B1-STRESS] n_docs={n} max_tokens={max_tokens}")
    print(f"[T10B1-STRESS] total_chunks={total_chunks} avg={avg:.1f} per-doc")
    print(f"[T10B1-STRESS] total_seconds={elapsed:.3f}s")
    print(f"[T10B1-STRESS] budget_violations={len(budget_violations)}")
    for doc_id, j, tk in budget_violations[:5]:
        print(f"  oversize: {doc_id} chunk[{j}] tokens={tk}")
    print(f"[T10B1-STRESS] failures={len(failures)}")
    for doc_id, msg in failures[:5]:
        print(f"  fail: {doc_id} {msg}")

    if budget_violations or failures:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
