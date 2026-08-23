# Phase 12 — v10 新批量入库验证报告(2026-08-23)

总口径: **2814 / 2825 成功(99.6%)** · 11 个 policy-blocked(>3000 chunks)· **C 类真实失败 = 0(五轮全零)**

## 1. 最终账目

| 项 | 值 |
|---|---|
| completed(checkpoint 终值) | **2814** |
| policy_blocked(P0-4 阈值 3000 拦截, 待 Phase 13 慢通道/预拆分) | **11**(实际 chunks 3000-7787, 清单 `/tmp/blocked_over3000.json` 去除已入库 1 个) |
| failed(真失败) | **0** |
| Qdrant v=2 points | **397,014** |
| chunk 分布(1297 个成功 log 样本) | P50=104 / P90=406 / P99=2469 / max=5724;>1500 的 31 doc |

## 2. 执行时间线(2026-08-21 14:15 → 08-23 14:41, 约 48.5h)

| 轮次 | 处理 | 成功 | 假失败 | 说明 |
|---|---|---|---|---|
| v10 主管道 | 1407 | 1090(146,931 chunks) | 317 | 两次风暴窗(15:17 原 wedge; 20:42 canary 撞活脚本) |
| v11 重试1 | 189 | 130(22,652) | 59 | 60s notify 退避生效, 双败率降至 31% |
| v12 重试2 | 28 | 0 | 28 | **全灭**: 落地病态 doc 排成 5h+ 串行编码车队, loop 连续阻塞; 容器重启解困 |
| v13 重试3 | 17(排除 12 blocked) | 7(8,122) | 10 | 10 个"失败"实为 B 类(已入库, 响应丢失) |
| v14 重试4 | 2 | 2(B 类确认) | 0 | 队列排干后 404 清零 |
| 最终分类 | 15 残余 | B=4 并入 | **A=11 = 恰好 policy_blocked** | 真瞬态清零 |

## 3. 关键发现(Phase 13 输入)

1. **C 类恒 0**: 管道(chunker→encode→Qdrant→FTS)在 ~2825 doc 上零真实失败; 全部失败均为控制面瞬态
2. **bge-m3 无界批次 OOM/wedge**(commit 4635e0c 修复): [1343,~512] 单批 attention 中间量 ~22.5GB
3. **估算式准入失效**: raw_chars/500 估算 vs OCR 病态行实际产出偏差 10-30×(679K chars 估 1359, 实际车队 5h); **准入必须查 chunk 后实际数**(chunker 0.04-0.24s/千 chunk, 零成本)
4. **编码实测速率**: 48 chunks/s 空闲态(2298-chunk/47.2s) vs 9.4 chunks/s 病态 doc; 单 doc 幂等 skip <5s
5. **status 端点空闲延迟 4.1s**(TaskRepo/审计索引冷路径) — P0-1 素材
6. **notify 假失败三源**: loop 阻塞窗 / 响应丢失(IncompleteRead)/ 排队中轮询超时 — 分类器 A/B/D 模型全部覆盖, 最终 D=0

## 4. 工具沉淀(commit 已推)

- `scripts/classify_ingest_failures.py` — A/B/C/D 分类(404 与传输错误分离, 10s 超时, 1.05s 限流节奏)
- `ingest_new_bundles.py` — 60s notify 退避 + IncompleteRead 可重试 + 动态 status 超时
- 探针模式: 容器内 parse+chunk 只读探实际 chunk 数(P0-4 准入的前身)

## 5. 残留

- 11 个 policy-blocked doc: >3000 chunks(最大 7787), 按 2026-08-23 用户拍板阈值拦截, 待 Phase 13 P2-1 预拆分或慢通道
- B 类(响应丢失但成功)的 callback 未送达 doc-to-md(https 校验拦截) — P1-5 对账项
- checkpoint 备份链: `/tmp/ingest_new_v4_checkpoint.json{,.bak-preB,.bak-preB2,.bak-preB3,.bak-preB4,.final-bak}`
