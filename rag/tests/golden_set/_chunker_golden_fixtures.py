"""Inline golden fixtures for chunker tests.

Constructs DocumentBlockIR lists that mimic the doc shapes which triggered
OOM under the legacy pure-char-offset chunker. Keeping fixtures inline
(no JSONL file IO) makes the test self-contained and lets it run in the
unit-test suite without fixture-path coupling.

Each fixture name mirrors a stress-test doc that exercised the failing
path; the exact text is reconstructed from typical engineering-doc
patterns (CJK + ASCII units + English technical text).
"""
from __future__ import annotations

from ekrs_shared.models import Content, DocumentBlockIR, Lineage, Metadata


def _make_block(
    block_id: str,
    type: str,
    raw: str,
    md_preview: str,
    heading_path: list[str] | None = None,
    page_number: int = 1,
    structured: list | None = None,
) -> DocumentBlockIR:
    return DocumentBlockIR(
        doc_id="golden_doc",
        block_id=block_id,
        type=type,
        content=Content(
            raw=raw, md_preview=md_preview, structured=structured,
        ),
        metadata=Metadata(
            page_number=page_number, heading_path=heading_path,
        ),
        lineage=Lineage(),
    )


def _long_text(text: str, repeat: int) -> str:
    """Replicate a short phrase N times to reach a target length."""
    if repeat <= 1:
        return text
    return (text + " ") * repeat


def large_pdf_blocks() -> list[DocumentBlockIR]:
    """~7.7k tokens in a single block — the canonical OOM-trigger."""
    # 30k chars ≈ 7.5k tokens at len/4
    text = _long_text(
        "压力容器设计应符合GB150标准，最高工作温度不超过350℃，"
        "最低工作温度不低于-40℃。设计压力不超过10MPa，"
        "水压试验压力为设计压力的1.5倍。",
        repeat=350,
    )
    return [
        _make_block(
            "lp1", "text", text, text,
            heading_path=["Ch1", "Sec1.1"],
        ),
    ]


def mixed_table_blocks() -> list[DocumentBlockIR]:
    """Text + table + text pattern; exercises table header propagation."""
    pre = _long_text(
        "材料力学性能应符合表1的规定。", repeat=20,
    )
    table_rows = [[f"param_{i}", str(i * 10), "MPa"] for i in range(120)]
    structured = [["参数", "标准值", "单位"]] + table_rows
    md = "| 参数 | 标准值 | 单位 |\n|------|--------|------|\n" + "\n".join(
        f"| param_{i} | {i * 10} | MPa |" for i in range(120)
    )
    post = _long_text(
        "上述参数适用于常温工况。高温工况应考虑蠕变效应。", repeat=20,
    )
    return [
        _make_block(
            "mt1", "text", pre, pre, heading_path=["Ch2"],
        ),
        _make_block(
            "mt2", "table", md, md, heading_path=["Ch2"], structured=structured,
        ),
        _make_block(
            "mt3", "text", post, post, heading_path=["Ch2"],
        ),
    ]


def chinese_legal_blocks() -> list[DocumentBlockIR]:
    """CJK-heavy text with explicit numeric constraints."""
    text = _long_text(
        "根据GB150.4-2011第3.2条规定，碳素钢和低合金钢制压力容器"
        "的设计温度范围为-20℃至400℃。奥氏体不锈钢制压力容器的"
        "设计温度上限可达800℃。当设计温度超过400℃时，应考虑"
        "持久强度和蠕变的影响。",
        repeat=200,
    )
    return [
        _make_block(
            "cl1", "text", text, text, heading_path=["法规引用"],
        ),
    ]


def english_tech_blocks() -> list[DocumentBlockIR]:
    """ASCII-heavy text with units and technical terms."""
    text = _long_text(
        "The pressure vessel shall be designed in accordance with "
        "ASME BPVC Section VIII. The maximum allowable working pressure "
        "is 10 MPa at design temperature 350 degrees Celsius. "
        "Hydrostatic test pressure shall be 1.5 times the design pressure. "
        "Materials shall conform to SA-516 Grade 70 with yield strength "
        "260 MPa minimum.",
        repeat=80,
    )
    return [
        _make_block(
            "et1", "text", text, text, heading_path=["Section 1"],
        ),
    ]


def stress_test_blocks() -> list[DocumentBlockIR]:
    """Medium-sized doc, typical stress-test ingestion shape."""
    blocks = []
    for i in range(10):
        text = _long_text(
            f"章节{i}：本节规定第{i}项设计要求。"
            f"最高温度350℃，最低温度-20℃，设计压力10MPa。",
            repeat=15,
        )
        blocks.append(_make_block(
            f"st{i}", "text", text, text,
            heading_path=[f"第{i}章"], page_number=i + 1,
        ))
    return blocks