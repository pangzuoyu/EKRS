# Phase 12 收尾 follow-ups (2026-08-21)

微批次修复 `4635e0c` 后的遗留项,按优先级:

## F1 — encode 移出 event loop(架构,先观察再动)
`upsert_chunks → EmbeddingService.encode → session.run` 同步跑在 loop 线程
(`qdrant_client.py:199` 链)。重 doc 编码期间 healthz 间歇超时(docker unhealthy streak)。

决策规则(2026-08-21 user): 先统计微批次修复后 healthz 超时频率 — 若 ≤1% doc 触发,
F1 降为低优先级; 若仍频繁, 实施**进程隔离**方案: `ProcessPoolExecutor` 而非
`asyncio.to_thread` — to_thread 的 future 在 ORT 死锁时永不完成且 ThreadPoolExecutor
无法中止 C 代码(线程残留); 子进程可 SIGKILL, 内存独立。注意 ProcessPool 下
2.2GB 模型需 per-process 加载或 fork 复用评估。

## F2 — ingest v10 跑完后的 failed 清理(操作,先分类后重试)
checkpoint `/tmp/ingest_new_v4_checkpoint.json`: failed 条目是
`{doc_hash, status, retries}` — `status=None` = notify 从未到终态(瞬态候选),
`status=failed/rejected` = 服务端真失败。**禁止直接全量重跑** — 用
`scripts/classify_ingest_failures.py --check-status` 分类:

- A: status=None 且 live≠success → transient, 经 `--include-list /tmp/fail_A_retry.json` 重试
- B: status=None 且 live=success → 服务端已有, 跳过(勿重发)
- C: checkpoint 或 live 为 failed/rejected → debug.log 逐条定位, 等用户决策
- D: live unreachable(忙/宕) → 稍后重跑分类器

pending 语义是 `not in completed`(failed 不阻重试), 所以 A-only 重试必须走
include-list, 不能靠改 checkpoint。全清后按 741/745 格式出
`deployment/phase12-v10-verification.md`。

## F3 — OCR 退化 doc 检索价值评估(数据)
1343-chunk doc(p50 长度 5 字符)已入库,chunk 带 `quality_warning=True`(d37efce)。
retriever 尚未消费该字段。

度量(2026-08-21 user): recall@10 报告中单独统计 top-10 里
`quality_warning=True` chunk 占比:
- 占比 ≤5% 且不伤 recall → 报告标注"已标识低质量 chunk,未来可降权", 不动代码
- 占比高且拖累 recall → 实施降权(score × 0.8)或上游 source-quality filter
