# 协调：oversized_image_block 2 bundle — OCR 端 image_description 文本输出超量（与 monolithic-tables coord 正交）

> **Audience**: doc-to-md 团队（OCR 模块 + image_classifier P3 owner）+ EKRS（admission + source-quality filter owner）
> **Author**: EKRS（Phase 13c-C13 D5 canary post-mortem）
> **Date**: 2026-08-28
> **Upstream**: [`docs/coordinations/2026-08-27-doc-to-md-monolithic-tables-and-fragmentation.md`](2026-08-27-doc-to-md-monolithic-tables-and-fragmentation.md) §9.9 + [`docs/solutions/integration-issues/ekrs-monolithic-tables-coord-reply-2026-08-27.md`](../solutions/integration-issues/ekrs-monolithic-tables-coord-reply-2026-08-27.md) §五.Q5
> **Splits from**: monolithic-tables coord §2.4 (oversized_image_block → 单独立项, 2026-08-27 EKRS 同意)
> **Failure data**: `deployment/phase13c-d5-canary-report.json` entries `8ab548bb51c076d0` + `ad58aff523d8d880`
> **Status**: **D0 — EKRS 侧 4 项答复完整 (2026-08-28), doc-to-md 实施开始**

---

## TL;DR

`oversized_image_block` 2 个 bundle 的失败根因**不在** monolithic-tables coord 文档的 §3 输出契约内，而是 doc-to-md OCR 端在 image-dominated 文档上输出**远超预期的 image_description 文本**（9.2 MB / 17.8 MB）。**D5-A 已 ship（2026-08-28 admission 永久 5M/10K, compose 改动在 `fe58d64`）**, EKRS admission 已放行 2 bundle；D5-GPU bypass canary 验证 2 bundle 100% PASS @ 7-10s/单 bundle, 但 chunk 数 1518/7117 仍让 encode latency 高于同类 (8-44s vs 7s avg for ≤500-chunk)。根因在 OCR 端输出超量, 不是 admission 或 channel.

**核心结论**:

| | bundle 1 (`8ab548bb51c076d0`) | bundle 2 (`ad58aff523d8d880`) |
|--|--|--|
| raw size | **9.2 MB** | **17.8 MB** |
| lines | 2,982 | 12,735 |
| 类别判断 | image-dominated (image>80% blocks) | 同上 |
| OCR 路径 | PaddleOCR-VL | MinerU 3.4.0 |
| 失败 gate | D5 前 raw>1M, D5-A (5M) ship 后✅ admission + GPU 100% PASS | D5 前 raw>1M, D5-A 后✅ admission + GPU 100% PASS |
| **遗留问题** | bge-m3 encode latency 23-30s, image_description 大量噪声 | 同上, 更严重 (47-56s) |

**修复方向**（与本文档契约**正交**, 单独立项追踪）:

1. **doc-to-md OCR 端**：image-dominated 文档上, image_description 输出应有**长度上限 + 语义截断** (e.g. `MAX_IMAGE_DESC_CHARS = 512` per image, 超长走 summary 路径)
2. **doc-to-md image_classifier P3**：阈值优化让 image-dominated 文档**早识别**, 走专门 image-region 路径 (而不是 OCR→text round-trip)
3. **EKRS source-quality filter**: ingest 前先做 `raw_chars > THRESHOLD` 旁路标记 `quality_warning="oversized_image_output"`, 决定降权 or 截断

---

## 一、背景

### 1.1 与 monolithic-tables coord 的边界

| 协调项 | 范围 | 当前状态 |
|--------|------|----------|
| monolithic-tables coord (2026-08-27) | single_table_monolith + tiny_content_fragmented + mixed_type_large + few_blocks_any + single_block_small | **D5 + D5-A COMPLETE, 62% pass** |
| **本文档 (oversized_image_block)** | 2 bundle, OCR 端 image_description 输出超量 | **D0 — 待双方 review** |

**为什么单独立项** (per coord reply §五.Q5 EKRS 同意):
> 根因在 OCR 端 (image 漏召), 不是输出契约. 这 2 个 bundle (`8ab548bb...` 3186 blocks / `ad58aff5...` 14451 blocks) 走的是 PaddleOCR-VL / MinerU 路径, 与本文档 §3 契约正交.

### 1.2 已 ship 的相关修复 (doc-to-md 侧)

| 修复 | commit | 影响 |
|------|--------|------|
| region OCR for chart/diagram 嵌入图 | `065e15b` (2026-08-13) | image-dominated 文档误判率 -6pp (47%→41%) |
| image_classifier P3 | 未 ship | 阈值优化 + heuristic 微调 (本文档诉求) |

---

## 二、根因分析（2 个 bundle）

### 2.1 `8ab548bb51c076d0` — PaddleOCR-VL 路径

**现象**: 9.2 MB raw / 2,982 lines
**OCR 行为**: PaddleOCR-VL 把每张 image 生成 1 个 `image_description` block, 全文 100+ image → 100+ 个长描述 block
**问题**:
- 单 image_description 平均 90 KB (远超正常 500 chars OCR text)
- 内容是 "image shows..." 自然语言描述, **不是 OCR 文字**
- EKRS bge-m3 encode 这些 image_description 是浪费 (语义检索不会命中)
- admission `5M raw` 已放行, 但 encode latency 23-30s 影响 GPU 队列

### 2.2 `ad58aff523d8d880` — MinerU 3.4.0 路径

**现象**: 17.8 MB raw / 12,735 lines
**OCR 行为**: MinerU middle_json `image_description` 字段全文 dump, 每个 image 一个 block
**问题**:
- 12,735 个 block, **远超同类 OCR 文档的 100-500 blocks 量级**
- 大量重复 description ("A photo of equipment..." "An industrial scene...")
- admission `5M` 已放行, encode latency 47-56s **接近 GPU timeout**

### 2.3 共性根因

**doc-to-md OCR 端缺 image_description 截断**:
- PaddleOCR-VL / MinerU 默认输出**完整** image_description
- doc-to-md 没有 `MAX_IMAGE_DESC_CHARS` 之类上限
- image-dominated 文档 (image>80% blocks) 上累计 raw size 远超正常

---

## 三、修复方向（三个方向并行）

### 3.1 方向 A — doc-to-md OCR 端加 image_description 截断（P0, 阻塞本文档 close）

**实施**: `parsers/ocr/{paddleocr_vl,mineru}.py` 输出后处理加 `MAX_IMAGE_DESC_CHARS` 阈值

```python
MAX_IMAGE_DESC_CHARS = 512  # EKRS 侧确认合理 (Q1 答复): 512 字符保留语义摘要, 大幅减少 raw size

def _truncate_image_desc(text: str) -> str:
    if len(text) <= MAX_IMAGE_DESC_CHARS:
        return text
    # 截断到 512 chars + "[... truncated ...]" marker
    # EKRS 侧确认 raw 字段保留完整作为审计追溯 (Q4 答复),
    # 截断状态通过 metadata.image_desc_truncated=True 标记
    truncated_text = text[:MAX_IMAGE_DESC_CHARS] + "\n[... truncated for embedding ...]"
    return truncated_text
```

**预期**: raw size -90% (90 KB → 0.5 KB per image), admission 远低于 5M, encode latency <5s

**owner**: doc-to-md OCR 模块维护者

**EKRS 侧承诺 (Q1+Q4 答复)**:
- ✅ 512 字符阈值合理, 保留语义摘要
- ✅ 截断后 raw 字段**必须**保留 (`metadata.image_desc_truncated=True` + `raw` 完整内容)
- D5 验证后根据实际效果调整阈值 (256 / 768 候选)

### 3.2 方向 B — doc-to-md image_classifier P3 阈值优化（P1, 不阻塞本文档 close）

**实施**: image>80% 文档走专门 image-region 路径, 不走 OCR→text round-trip

**已知进展**: `065e15b` (2026-08-13) 已 ship region OCR, 但阈值保守
**继续方向**: 阈值从 80% 降到 60% 触发, 让更多 image-dominated 文档早识别

**owner**: doc-to-md image_classifier P3 owner

**EKRS 侧条件 (Q2 答复)**: ⚠️ doc-to-md 侧需在 corpus 上跑回归测试, **假阳性率 ≤5%** (正常 PDF 被误判为 image-dominated 的比例) 后才 ship. EKRS 侧无明确反对, 但要求验证阈值.

### 3.3 方向 C — EKRS source-quality filter（P2, 不阻塞本文档 close）

**实施**: ingest 前对 raw>5M bundle 加 `quality_warning` 标记, EKRS 端可选降权

```python
# backend/engine/admission.py 入口处, 与 raw_chars 检查并列 (Q3 答复)
if raw_chars > 5_000_000:
    bundle.metadata.quality_warning = "oversized_image_output"
    # step5_worker.py encode 阶段: quality_warning=True 时降权 (而非完全丢弃)
    # 截断状态标记: metadata.image_desc_truncated=True 时同样降权
```

**owner**: EKRS RAG team (per coord reply §七.1)

**EKRS 侧答复 (Q3)**: ✅ 实施位置确定 — `backend/engine/admission.py` 入口, 与 raw_chars 检查并列. 标记 `quality_warning` 后, `step5_worker.py` encode 阶段跳过/降权. **不阻塞本文档**, Phase 13c 或 Phase 14 独立迭代.

---

## 四、时间表（待 doc-to-md 确认）

| Day | 日期 | 责任 | 内容 |
|-----|------|------|------|
| D0 | 2026-08-28 (四) | EKRS | **本文档 ship**, 双方 review 修复方向 |
| D1 | 2026-08-29 (五) | doc-to-md | 方向 A: MAX_IMAGE_DESC_CHARS=512 截断, 单测 5 个 (PaddleOCR-VL + MinerU) |
| D2 | 2026-09-01 (一) | doc-to-md | `repair_2_oversized_bundles.py` 写完, 重输出 `8ab548bb` + `ad58aff5` 到 `/mnt/disk/text/v1.1/` |
| D3 | 2026-09-02 (二) | 联调 | EKRS 50-bundle canary 含 2 bundle, 验证 raw size <500K + encode latency <10s |
| D4 | 2026-09-03 (三) | doc-to-md | 方向 B: image_classifier P3 阈值优化 (60%) |
| D5 | 2026-09-04 (四) | 联调 | EKRS 全 corpus re-ingest, 2 bundle 100% 通过 |
| D6 | 2026-09-05 (五) | 联调 | 本文档 close, 写 closure status |

**关键节点**:
- **D0 end-of-day**: doc-to-md owner 确认方向 A 是否可 ship
- **D2 end-of-day**: doc-to-md 给 EKRS 2 bundle 重输出路径, EKRS 配置 SHARED_STORAGE_PATH
- **D5 mid-day**: 灰度报告. 若 2/2 通过, 进 D6 close; 若 <2/2, doc-to-md 补 fix

---

## 五、验收标准

| 指标 | 修前 | 目标 |
|------|------|------|
| `8ab548bb51c076d0` raw size | 9.2 MB | <500 KB |
| `ad58aff523d8d880` raw size | 17.8 MB | <1 MB |
| `8ab548bb` lines | 2,982 | <500 |
| `ad58aff5` lines | 12,735 | <2000 |
| bge-m3 encode latency (per bundle) | 23-56s | <10s |
| EKRS admission gate | ✅ 5M/10K (D5-A) | 永久 |
| 总 96 bundle 闭环率 | 31/50 = 62% (D5 canary sample) | ≥ 33/50 = 66% (含 2 image bundle) |

**注**: 96 bundle 全量闭环率预期从 62% → ~65-67%. 剩余 admission-gated bundle 已通过 **D5-A admission 5M/10K 永久 ship** (commit `fe58d64`, `deployment/docker-compose.yml` + `deployment/docker-compose.override.yml`) 全部 ingest path 开放 — 无需另立 admission 协调项. 本文档 focus 在 OCR 端 image_description 截断 (方向 A/B/C).

---

## 六、doc-to-md 端交付清单

1. **`parsers/ocr/paddleocr_vl.py` patch** — image_description 截断 (D1)
2. **`parsers/ocr/mineru.py` patch** — 同上 (D1)
3. **`parsers/ocr/_truncate_image_desc()` helper** — 公共方法, 单测覆盖 (D1)
4. **`scripts/repair_2_oversized_bundles.py` (新)** — 重输出工具 (D2)
5. **测试** ≥ 5 单测, 覆盖 2 OCR 路径截断 (D1)
6. **2 bundle 重产物** in `/mnt/disk/text/v1.1/{8ab548bb51c076d0,ad58aff523d8d880}/` (D2)

---

## 七、EKRS 端承诺

1. **不动 Qdrant**, `pipeline.ingest(version=3)` 增量 ingest (D3, D5)
2. **写 `scripts/c13_oversized_image_verify.py`**, 按 §五验收标准出 report (D3)
3. 若 2 bundle 验收未达预期, 逐 doc 复盘 (D5)
4. **不接受** doc-to-md 在源文件不可用时用 stale 旧 bundle 凑数

---

## 八、未决问题 — EKRS 侧答复 (2026-08-28)

> **状态**: EKRS 侧 4 项问题已全部答复 (✅✅⚠️✅). doc-to-md 侧按答复实施.

| # | 问题 | EKRS 答复 | doc-to-md 实施要点 |
|---|------|----------|------------------|
| 1 | 方向 A (MAX_IMAGE_DESC_CHARS=512) 阈值是否合理? | ✅ **合理**. 512 字符保留 image_description 语义摘要, 大幅减少 raw size. 截断时保留前 512 + `[... truncated for embedding ...]` marker. D5 验证后可调整 (256 / 768) | `parsers/ocr/_truncate_image_desc()` 常量设为 512. 单测覆盖 PaddleOCR-VL + MinerU 2 路径. D5 后回归调整 |
| 2 | 方向 B image_classifier 阈值 80% → 60% 是否过早? | ⚠️ **需回归测试**. 假阳性率 ≤5% (正常 PDF 误判为 image-dominated) 才 ship. EKRS 侧无明确反对 | doc-to-md 侧在 corpus 上跑回归测试, 出 false-positive rate 报告 (target ≤5%) |
| 3 | 方向 C EKRS source-quality filter 实施位置? | ✅ `backend/engine/admission.py` 入口, 与 raw_chars 检查并列. 标记 `quality_warning` 后 `step5_worker.py` encode 阶段降权. **不阻塞本文档**, Phase 13c 或 Phase 14 独立迭代 | 不阻塞本文档. 单独 Phase 14 任务追踪 |
| 4 | 截断后是否需要保留 raw 字段做 fallback? | ✅ **需要**. `metadata.image_desc_truncated=True` 标记, raw 字段保留完整内容作为审计追溯. EKRS 端 `quality_warning=True` 降权, 不完全丢弃 | doc-to-md 侧 output block 同时保留: `content.raw` 完整 + `content.md_preview` 截断 + `metadata.image_desc_truncated=True` |

---

## 九、资源索引

- 上游协调: [`2026-08-27-doc-to-md-monolithic-tables-and-fragmentation.md`](2026-08-27-doc-to-md-monolithic-tables-and-fragmentation.md) §9.9 (D5-A SHIPPED)
- 协调答复: [`../solutions/integration-issues/ekrs-monolithic-tables-coord-reply-2026-08-27.md`](../solutions/integration-issues/ekrs-monolithic-tables-coord-reply-2026-08-27.md) §五.Q5
- D5 canary 报告: `deployment/phase13c-d5-canary-report.json` (entries `8ab548bb51c076d0`, `ad58aff523d8d880`)
- D5-GPU bypass 报告: `deployment/phase13c-d5-gpu-bypass-15-report.json` (含 2 bundle @ 5M/10K)
- 已 ship OCR 修复: `065e15b` (2026-08-13 region OCR for chart/diagram)
- 关联内存: `region-ocr-mixed-image.md`, `ekrs-content-hash-p2-demotion.md`

---

## 十、元数据

- 协调请求方: EKRS（Phase 13c-C13 D5 canary post-mortem）
- 接收方: doc-to-md（OCR 模块 + image_classifier P3 owner）+ EKRS RAG team
- Owner: 方向 A/B = doc-to-md OCR; 方向 C = EKRS RAG
- 状态: **D0 — 待 doc-to-md review 修复方向**
- 版本: v0.1 (2026-08-28, 初始草稿)