# Phase 12 收尾 follow-ups (2026-08-21)

微批次修复 `4635e0c` 后的遗留项,按优先级:

## F1 — encode 移出 event loop(架构,缓)
`upsert_chunks → EmbeddingService.encode → session.run` 同步跑在 loop 线程
(`qdrant_client.py:199` 链)。重 doc 编码期间 healthz 间歇超时(docker unhealthy streak)。
方案: `asyncio.to_thread` 化 encode;注意真正 ORT 死锁场景下 to_thread future 不会完成,
需配 per-batch 超时。等 ingest 全量跑完、失败清单清零后再动。

## F2 — ingest v10 跑完后的 failed 清理(操作)
checkpoint `/tmp/ingest_new_v4_checkpoint.json`: failed 含 37 真 + ~106 假
(canary 期间 loop 阻塞误标)。`pending = not in completed` → 直接重跑脚本即可重试全部 failed。
验完按 741/745 报告格式出验证数据。

## F3 — OCR 退化 doc 检索价值评估(数据)
1343-chunk doc(p50 长度 5 字符)已入库,chunk 带 `quality_warning=True`(d37efce)。
retriever 尚未消费该字段。跑 recall@10 时对比此类 doc 的贡献,再决定是否
降权/上游 source-quality filter(doc-to-md 侧建议,暂缓)。
