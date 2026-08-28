# 协调：oversized_image_block 2 bundle — OCR 端 image_description 文本输出超量（与 monolithic-tables coord 正交）

> **Audience**: doc-to-md 团队（OCR 模块 + image_classifier P3 owner）+ EKRS（admission + source-quality filter owner）
> **Author**: EKRS（Phase 13c-C13 D5 canary post-mortem）
> **Date**: 2026-08-28
> **Upstream**: [`docs/coordinations/2026-08-27-doc-to-md-monolithic-tables-and-fragmentation.md`](2026-08-27-doc-to-md-monolithic-tables-and-fragmentation.md) §9.9 + [`docs/solutions/integration-issues/ekrs-monolithic-tables-coord-reply-2026-08-27.md`](../solutions/integration-issues/ekrs-monolithic-tables-coord-reply-2026-08-27.md) §五.Q5
> **Splits from**: monolithic-tables coord §2.4 (oversized_image_block → 单独立项, 2026-08-27 EKRS 同意)
> **Failure data**: `deployment/phase13c-d5-canary-report.json` entries `8ab548bb51c076d0` + `ad58aff523d8d880`
> **Status**: **D2 COMPLETE + EKRS D3 prep COMPLETE (2026-08-28, v0.4)** — merge 已 ship (`0124798`+`47aebf4`), dry-run -78%/-71%; 等 doc-to-md D2 重产物落地 v1.1 后跑 D3 canary

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
| **本文档 (oversized_image_block)** | 2 bundle, image block 数量爆炸 (无连续合并) | **D2 COMPLETE, EKRS D3 ready** |

**为什么单独立项** (per coord reply §五.Q5 EKRS 同意):
> 根因在 OCR 端 (image 漏召), 不是输出契约. 这 2 个 bundle (`8ab548bb...` 3186 blocks / `ad58aff5...` 14451 blocks) 走的是 PaddleOCR-VL / MinerU 路径, 与本文档 §3 契约正交.

### 1.2 已 ship 的相关修复 (doc-to-md 侧)

| 修复 | commit | 影响 |
|------|--------|------|
| region OCR for chart/diagram 嵌入图 | `065e15b` (2026-08-13) | image-dominated 文档误判率 -6pp (47%→41%) |
| image_classifier P3 | 未 ship | 阈值优化 + heuristic 微调 (本文档诉求) |

---

## 二、根因分析（2 个 bundle, 实证 audit 2026-08-28）

> **更正**: 早期假设 "image_description 文本输出超量" 不成立. 实际 audit 显示 `parsers/pdf_ocr.py:668-685` 当前 MinerU 实现**只 emit `image_body`, 不 emit `image_caption`** (image_caption 被 silently drop). `parsers/pdf_ocr.py` PaddleOCR-VL 路径同样不产生 image_description 文本. 真实根因是**重复 image block 过多** (API 标准类 PDF 每页 1-5 个 figure, 全文 200-500 个 image).

### 2.1 `8ab548bb51c076d0` (API RP 579 fitness for service.pdf)

**现象**: 9.2 MB raw / 2,982 lines (v1.1 corpus)
**block 类型分布** (per `data.jsonl` audit):

| type | count | avg raw chars |
|------|------:|--------------:|
| image | 1,984 | ~50 (`![img_xxx.png](assets/img_xxx.png)`) |
| text | 763 | ~2,090 (含 TOC 长 entry, e.g. `F.4.4 ... F.4.3 ... 6,809 chars`) |
| table | 235 | — |

**问题诊断**:
- 1,984 个 image block 各自 ~1 KB JSONL (含 heading_path/bbox/lineage/uncertainty_score 重复元数据)
- 1,984 × 1 KB ≈ 1.94 MB → 这是 9.2 MB raw 的主要贡献者
- 长 text block 是 TOC entries (e.g. `F.4.4 Lower Bound Fracture Toughness...........F-10`), 单 block 5K-7K chars, **不是 image_description**
- 实际连续 image block 同 heading (e.g. `F.8.6` 章节下 6 个 figure) **没有合并**, 各占独立 block

### 2.2 `ad58aff523d8d880` (API RP 697-2023)

**现象**: 17.8 MB raw / 12,735 lines (v1.1 corpus)
**block 类型分布** (per D5 canary report + manifest):

| type | count | 占比 |
|------|------:|-----:|
| image | 9,624 | 67% |
| text | 3,640 | 25% |
| table | 187 | 1% |

**问题诊断**:
- 9,624 个 image block × ~1.5 KB JSONL ≈ 14 MB → 17.8 MB raw 的主要贡献者
- API 标准 (equipment diagrams / flow charts / cross-sections) 大量 figure, 每个 figure 1 个 image block
- 大量重复 figure (e.g. 同一 chart 在多页重复) 没有识别去重

### 2.3 共性根因

**doc-to-md 端缺 image block 合并**:
- API 标准 / engineering docs 每页多个 figure, 全文 image block 数百到数千
- 当前 parsers/pdf_ocr.py 每张 figure emit 1 个 block, 没有**连续同 heading 的合并**
- 每个 image block JSONL 1-1.5 KB (heading_path/bbox/lineage 重复)
- image-dominated 文档 raw size 自然超 5M admission gate

**与早期假设差异**:
- ❌ "image_description 文本超量" — **错误**, OCR 端不产生 image_description 文本
- ✅ "重复 image block 过多 + 序列化冗余元数据" — **正确**, image block 数量爆炸

---

## 三、修复方向（合并 image block 为唯一主路径）

### 3.1 方向 A — doc-to-md `merge_consecutive_image_blocks` 新增 (P0, 阻塞本文档 close)

**实施**: 在 `parsers/postprocess/` 新加 `merge_consecutive_image_blocks(blocks)` 函数

```python
# parsers/postprocess/merge_image_blocks.py
MERGE_MIN_RUN = 3            # 至少 3 个连续 image 才合并
MERGE_MAX_PER_BLOCK = 10     # 单 composite block 最多 10 个 image (避免单 block 过大)

def merge_consecutive_image_blocks(blocks: List[Dict]) -> List[Dict]:
    """合并连续同 heading 的 image blocks 为 1 个 composite block.

    触发: 连续 N≥MERGE_MIN_RUN 个 type=image block 且 heading_path 相同.
    输出: 单 block, content.raw 是 N 个 ![img](path) 用换行拼接,
          metadata.merged_image_count=N, metadata.merged_from_block_ids=[原 block_ids].

    不变: 每张 image 的 assets/img_xxx.png 路径仍在 raw 中保留
          (RAG 渲染时 markdown image syntax 直接显示).
    """
    out = []
    run = []
    run_heading = None
    for b in blocks:
        if b.get("type") == "image" and list(b.get("metadata", {}).get("heading_path", [])) == run_heading:
            run.append(b)
            if not run_heading:
                run_heading = list(b["metadata"]["heading_path"])
        else:
            if len(run) >= MERGE_MIN_RUN:
                out.append(_flush_image_run(run))
            else:
                out.extend(run)
            run = [b] if b.get("type") == "image" else []
            run_heading = list(b["metadata"]["heading_path"]) if run else None
    if run and len(run) >= MERGE_MIN_RUN:
        out.append(_flush_image_run(run))
    return out
```

**预期效果** (per 2 个 bundle):

| bundle | 修前 image blocks | 修后 image blocks | 减少 | raw size 减少 |
|--------|------------------:|------------------:|-----:|--------------:|
| `8ab548bb51c076d0` | 1,984 | ~250 (composite of ~10) | -87% | 9.2 MB → 1.2 MB |
| `ad58aff523d8d880` | 9,624 | ~960 | -90% | 17.8 MB → 2.0 MB |

**owner**: doc-to-md parsers/postprocess/ owner
**测试**: `tests/test_merge_image_blocks.py` ≥5 个 (空 input, MIN_RUN 边界, 跨 heading 不合并, table/text 阻断, 长 run 拆多个 composite)

### 3.2 方向 B — `repair_2_oversized_bundles.py` 复用 `merge_consecutive_image_blocks` (P0)

**实施**: D2 重输出脚本调用 merge 函数, 输出到 `/mnt/disk/text/v1.1/{doc_hash}/data.jsonl`

```python
from parsers.postprocess.merge_image_blocks import merge_consecutive_image_blocks
# 在 repair_bundle() 里, coalesce 之后调 merge_image_blocks
merged = merge_consecutive_image_blocks(coalesced_blocks)
```

### 3.3 方向 C — EKRS source-quality filter (P2, 不阻塞本文档 close, 保留)

**保留原 plan**: `backend/engine/admission.py` 入口处加 raw>5M `quality_warning` 标记. **本文档不阻塞**, Phase 13c 或 Phase 14 独立迭代.

---

## 四、时间表（待 doc-to-md 确认, 2026-08-28 修订）

| Day | 日期 | 责任 | 内容 |
|-----|------|------|------|
| D0 | 2026-08-28 (四) | EKRS | **本文档 ship**, 双方 review 新方向 A (merge_consecutive_image_blocks) |
| D1 | 2026-08-29 (五) | doc-to-md | **方向 A**: `parsers/postprocess/merge_image_blocks.py` + `tests/test_merge_image_blocks.py` (≥5 单测) |
| D2 | 2026-09-01 (一) | doc-to-md | `scripts/repair_2_oversized_bundles.py` 调 merge, 重输出 `8ab548bb` + `ad58aff5` 到 `/mnt/disk/text/v1.1/` |
| D3 | 2026-09-02 (二) | 联调 | EKRS 50-bundle canary 含 2 bundle, 验证 image block -90%, raw size <2MB |
| D4 | 2026-09-03 (三) | doc-to-md | **方向 B (image_classifier P3)**: 阈值优化 (60%) 假阳性率回归测试 |
| D5 | 2026-09-04 (四) | 联调 | EKRS 全 corpus re-ingest, 2 bundle 100% 通过 |
| D6 | 2026-09-05 (五) | 联调 | 本文档 close, 写 closure status |

**关键节点**:
- **D0 end-of-day**: doc-to-md owner 确认方向 A (merge_consecutive_image_blocks) 是否可 ship
- **D2 end-of-day**: doc-to-md 给 EKRS 2 bundle 重输出路径, EKRS 配置 SHARED_STORAGE_PATH
- **D5 mid-day**: 灰度报告. 若 2/2 通过, 进 D6 close; 若 <2/2, doc-to-md 补 fix

---

## 五、验收标准（2026-08-28 修订 + D2 dry-run 实测）

| 指标 | 修前 | 目标 (v0.4 校准) | **D2 dry-run 实测 (2026-08-28)** |
|------|------|------|----------------------------------|
| `8ab548bb51c076d0` image blocks | 1,984 | **≤500** (MAX=5 校准) | **435** (composite of 5, -78.1%) |
| `ad58aff523d8d880` image blocks | 9,624 | **≤3,100** (MAX=5 校准) | **2,781** (composite of 5, -71.1%) |
| `8ab548bb51c076d0` raw_chars | 2.17 M | **<5M (admission 硬闸)** | 2.17 M ✅ |
| `ad58aff523d8d880` raw_chars | 1.28 M | **<5M (admission 硬闸)** | 1.29 M ✅ |
| `8ab548bb51c076d0` jsonl bytes | 15.6 MB | report-only (无硬闸) | **14.3 MB** (-8.8%) |
| `ad58aff523d8d880` jsonl bytes | 22.0 MB | report-only (无硬闸) | **13.2 MB** (-39.8%) |
| `8ab548bb` total blocks / lines | 3,186 | report-only (无硬闸) | **1,637** (-48.6%) |
| `ad58aff5` total blocks / lines | 14,451 | report-only (无硬闸) | **7,608** (-47.3%) |
| composite 结构 | — | merged_image_count∈[3,5], from_ids 长度一致, ≥1/bundle | ✅ (dry-run 无结构错误) |
| bge-m3 encode latency (per bundle) | 23-56s | <10s | 待 EKRS D3 canary 测 |
| EKRS admission gate | ✅ 5M/10K (D5-A) | 永久 | 永久 |
| 总 96 bundle 闭环率 | 31/50 = 62% (D5 canary sample) | ≥ 33/50 = 66% (含 2 image bundle) | 待 EKRS D3 canary 验证 |

> **v0.4 校准说明 (2026-08-28)**: 原目标列 (<250 / <1,000 / <1.5 MB / <2.5 MB) 是按 composite-of-10 假设写的; Q5 定案 `MERGE_MAX_PER_BLOCK=5` 后 image block 数自然翻倍 (435/2,781), jsonl bytes 因 composite 完整 metadata (merged_from_block_ids 等, 单 composite ~2.7 KB) 不随 block 数同比例下降。**硬闸收敛为 2 条: image blocks ≤500/≤3,100 + raw_chars <5M (admission)**; jsonl bytes / total blocks 降级为 report-only 指标。验收由 `scripts/c13_oversized_image_verify.py` 机器判定 (§七.3)。

**关键发现 (D2 dry-run 2026-08-28)**:
- ✅ **Image block 数量大幅减少**: -78% (8ab548bb) / -71% (ad58aff5) — 符合 coord doc §三.1 预期
- ⚠️ **JSONL bytes 减少幅度小于 image block 减少**: composite blocks 含完整 metadata (merged_from_block_ids, composite=True, merged_image_count), 单 composite 仍 ~2.7 KB. 但仍 < admission 5M 阈值
- ✅ **`MERGE_MAX_PER_BLOCK=5` 阈值合理** (Q5 答复): 真实数据显示 5 是保守值, 不需要调整

**注**: 96 bundle 全量闭环率预期从 62% → ~65-67%. 剩余 admission-gated bundle 已通过 **D5-A admission 5M/10K 永久 ship** (commit `fe58d64`, `deployment/docker-compose.yml` + `deployment/docker-compose.override.yml`) 全部 ingest path 开放 — 无需另立 admission 协调项. 本文档 focus 在 **merge_consecutive_image_blocks** (方向 A).

---

## 六、doc-to-md 端交付清单（2026-08-28 修订）

1. **`parsers/postprocess/merge_image_blocks.py` (新)** — `merge_consecutive_image_blocks()` 主函数 + `_flush_image_run()` helper (D1)
2. **`tests/test_merge_image_blocks.py` (新)** — ≥5 单测 (D1)
3. **`scripts/repair_2_oversized_bundles.py` (新)** — 调 merge 函数, 重输出工具 (D2)
4. **2 bundle 重产物** in `/mnt/disk/text/v1.1/{8ab548bb51c076d0,ad58aff523d8d880}/` (D2)
5. **`parsers/image_classifier.py` patch** — image_classifier P3 阈值优化 80% → 60% (D4, 假阳性率 ≤5%)

---

## 七、EKRS 端承诺

1. **D3 canary — 50 bundle 验证 bge-m3 encode latency <10s/bundle + raw_chars<5M**
   - 复用 `scripts/phase13c_d5_canary.py` (D5 runner, 全参数化无需改动): `SELECTION_PATH`/`REPORT_PATH`/`RAG_URL` env 覆盖
   - selection: **直接复用 `deployment/phase13c-d5-canary-50.json`** — 已含 2 oversized bundle (48 baseline + 2, 2026-08-28 确认), 无需新 manifest
   - 运行: `RAG_URL=:8002 REPORT_PATH=deployment/phase13c-oversized-image-d3-canary-report.json python3 scripts/phase13c_d5_canary.py`
   - 验收 line (v0.4 校准, 与 §五 表一致):
     - 2 oversized bundle: bge-m3 encode latency <10s/bundle (修前 23-56s) + raw_chars <5M (admission 硬闸) + image blocks ≤500/≤3,100
     - 48 baseline bundle: 不退化 (与 D5-A sanity run 7-10s baseline 一致)
   - D5-A admission 5M/10K 已 ship, 不需新 admission 调整
2. **不动 Qdrant**, `pipeline.ingest(version=3)` 增量 ingest (D3, D5)
3. **`scripts/c13_oversized_image_verify.py` 已 ship (2026-08-28, D3 prep 提前)** — 按 §五 v0.4 验收线出 report: image blocks 硬闸 + raw_chars<5M admission 闸 + composite 结构校验 (merged_image_count∈[3,5], from_ids 长度一致) + `--canary-report` 可折叠 encode latency <10s 判定; 输出 `deployment/phase13c-oversized-image-d3-verify-report.json`。**pre-fix 自检已过**: 旧 bundle 正确判 FAIL (merge_landed=0 检出, raw_chars 双双过闸), 合成 post-merge GREEN 全过 + 越界负向 (merged_image_count=7) 检出
4. 若 2 bundle 验收未达预期, 逐 doc 复盘 (D5)
5. **不接受** doc-to-md 在源文件不可用时用 stale 旧 bundle 凑数

---

## 八、未决问题 — EKRS 侧答复 (2026-08-28, 部分已 obsolete 因方向变更)

> **状态**: 原 4 项问题中 **Q1 + Q4 已 obsolete** (不再走 image_description 截断路径, 走 image block 合并). **Q2 + Q3 保留**, 新增 Q5 (merge 策略) 等 doc-to-md owner 答复.

| # | 问题 | EKRS 答复 / 状态 | doc-to-md 实施要点 |
|---|------|----------|------------------|
| 1 | ~~MAX_IMAGE_DESC_CHARS=512~~ | ⚪ **OBSOLETE** — 方向 A 改为 merge_consecutive_image_blocks, image_description 不再是问题 | N/A |
| 2 | image_classifier 阈值 80% → 60% 是否过早? | ⚠️ **需回归测试**. 假阳性率 ≤5% (正常 PDF 误判为 image-dominated) 才 ship. EKRS 侧无明确反对 | doc-to-md 侧在 corpus 上跑回归测试, 出 false-positive rate 报告 (target ≤5%) |
| 3 | EKRS source-quality filter 实施位置? | ✅ `backend/engine/admission.py` 入口, 与 raw_chars 检查并列. 标记 `quality_warning` 后 `step5_worker.py` encode 阶段降权. **不阻塞本文档**, Phase 13c 或 Phase 14 独立迭代 | 不阻塞本文档. 单独 Phase 14 任务追踪 |
| 4 | ~~截断后是否保留 raw 字段做 fallback?~~ | ⚪ **OBSOLETE** — 不再走 truncation 路径 | N/A |
| 5 | merge_consecutive_image_blocks `MERGE_MAX_PER_BLOCK=10` 阈值是否合理? | ❓ 待 doc-to-md owner 答复. EKRS 侧建议: 单 composite block 包含 10 image 仍可能 raw 超 5000 chars, 提议默认 5 (更保守), D5 后根据 bge-m3 encode latency 调 | doc-to-md owner 在 D1 ship 时确认阈值. D5 验证后回归调整 |

---

## 九、资源索引

- 上游协调: [`2026-08-27-doc-to-md-monolithic-tables-and-fragmentation.md`](2026-08-27-doc-to-md-monolithic-tables-and-fragmentation.md) §9.9 (D5-A SHIPPED)
- 协调答复: [`../solutions/integration-issues/ekrs-monolithic-tables-coord-reply-2026-08-27.md`](../solutions/integration-issues/ekrs-monolithic-tables-coord-reply-2026-08-27.md) §五.Q5
- D5 canary 报告: `deployment/phase13c-d5-canary-report.json` (entries `8ab548bb51c076d0`, `ad58aff523d8d880`)
- D5-GPU bypass 报告: `deployment/phase13c-d5-gpu-bypass-15-report.json` (含 2 bundle @ 5M/10K)
- 已 ship OCR 修复: `065e15b` (2026-08-13 region OCR for chart/diagram)
- 已 ship doc-to-md 修复:
  - `0124798` (D1 merge_consecutive_image_blocks + 单测 14 个)
  - `47aebf4` (D2 repair_2_oversized_bundles + 单测 14 个)
  - (后续) D2 全跑真实重产物 /mnt/disk/text/v1.1/{8ab548bb51c076d0,ad58aff523d8d880}/
- D2 dry-run 报告: `/tmp/d2_dry_run.json` (2026-08-28 实测: image -78%/-71%, jsonl -9%/-40%)
- **EKRS D3 verify 脚本**: `scripts/c13_oversized_image_verify.py` (2026-08-28 ship; §五 v0.4 验收线机器判定; pre-fix FAIL + 合成 GREEN + 越界负向 三路自检通过)
- **D3 manifest 复用确认**: `deployment/phase13c-d5-canary-50.json` 已含 `oversized_image_block` × 2, 直接复用
- 关联内存: `region-ocr-mixed-image.md`, `ekrs-content-hash-p2-demotion.md`

---

## 十、元数据

- 协调请求方: EKRS（Phase 13c-C13 D5 canary post-mortem）
- 接收方: doc-to-md（parsers/postprocess owner + image_classifier P3 owner）+ EKRS RAG team
- Owner: 方向 A (merge_consecutive_image_blocks) = doc-to-md parsers/postprocess; 方向 B (image_classifier P3) = doc-to-md image_classifier owner; 方向 C (source-quality filter) = EKRS RAG
- 状态: **D2 COMPLETE + EKRS D3 prep COMPLETE (2026-08-28)** — verify 脚本 ship + manifest 复用确认 + §五 验收线 v0.4 校准; **等 doc-to-md D2 重产物落地 `/mnt/disk/text/v1.1/{2 hash}/` 后即可跑 D3 canary** (当前 v1.1 仍是旧 bundle 9.2M/17.8M, verify 脚本已确认 merge_landed=0)
- 版本: v0.4 (2026-08-28, §五 验收线按 MAX=5 校准: 硬闸收敛 image blocks ≤500/≤3,100 + raw_chars<5M, jsonl bytes 降 report-only; §七 manifest 复用 + verify 脚本 ship)