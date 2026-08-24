# Phase 13b T5 — E2E 验收套件

## Context

T1 (7c7377c) + T2+T4 (b8a03b1) + T3 (8f2563d) 落了 torch FP16 GPU encode + EncodingRouter + 30s 探针 + channel_switched audit。T5 是真实基础设施验收 — 28 篇 Phase 12 v10 子集，跑 GPU↔CPU 等价 + 故障转移 ≤30s。基线: RAG 已 build, qdrant+redis+rag healthy, `BGE_M3_GPU_ENABLED=False` 默认, `BGE_M3_GPU_PROBE_INTERVAL_S=30` 默认。

## Files

新增 5 + 改 1：
- `scripts/_phase13b_common.py` NEW — `read_corpus` + `build_notify_payload` 提取 (复用 live_stress_60.py:511-548, 632-645)
- `scripts/phase13b_poc_bench.py` NEW — T5.1
- `scripts/phase13b_equiv_check.py` NEW — T5.2
- `scripts/phase13b_failover_test.py` NEW — T5.3
- `tests/integration/test_phase13b_t5_e2e.py` NEW @pytest.mark.heavy — T5.4 套件入口
- `tests/unit/test_phase13b_t5_acceptance.py` NEW — T5.5 纯 Python 桩
- `Makefile` 增 `make t5-acceptance`
- `rag/ekrs_rag/api/routes/admin.py` 加 2 endpoints (eng-review fix #3 + risk #6):
  - `POST /v1/admin/gpu/invalidate` (T5.3 触发点)
  - `POST /v1/admin/gpu/memory-stats` (T5.1 精确 GPU mem 读取)
- `scripts/_phase13b_poc_28doc_fallback.txt` NEW (risk #1) — Phase 12 v10 verification 实际跑过的 28 doc_hash 列表, 预检不足时兜底
- `deployment/phase12-recall-gt.json` NEW (risk #2) — `{doc_hash: {query: recall}}`, T5.2 ground truth 直接 JSON load
- `scripts/_phase13b_common.reset_state()` 函数 (建议) — Qdrant collection delete/recreate + FTS 文件 unlink, 封装 wipe 逻辑

每个脚本要 `def run(...) -> Report` 可被 pytest import。

## Steps

### T5.1 phase13b_poc_bench.py

**Drain-first (eng-review fix #1)**: 进入 Phase B restart 前, poll 全部 28 个 doc_hash 的 `/v1/ingestion/status`, **300s timeout (risk #5: 120s 不够), --force 可绕过**, 每 5s 打印 `status: X/total success, Y/failed, Z/pending` 便于调试. 防 `task_repo` 重排队 race.

**28 篇预检 (risk #1)**: 启动时遍历 corpus_root, 过滤 `(dir / 'data.jsonl').exists() AND data.jsonl.size > 0`. 取前 28. 若 < 28, 退回 `scripts/_phase13b_poc_28doc_fallback.txt` (T5.1 首次跑前生成 — 记录 Phase 12 v10 verification 实际跑过的 28 个 doc_hash).

**冒烟基准 (risk #4)**: Phase B 跑前先灌单篇大文档 (取 28 篇里 chunk 数最多的), 测端到端 wall-time. 若 >25s (留 5s 余量) 提前 `WARNING: 预估 7787-chunk 文档可能超过 30s, 建议 `export T5_PERF_OVERRIDE_LARGEST_DOC_S=60` 或调整 `_BATCH_SIZE``. 阈值从环境变量读, 默认 30s.

**Wipe 封装 (建议)**: `_phase13b_common.reset_state()` 干两事:
```python
def reset_state(qdrant_url: str, fts_path: Path):
    # Qdrant
    httpx.delete(f"{qdrant_url}/collections/rag_documents")
    httpx.put(f"{qdrant_url}/collections/rag_documents", json={...})  # recreate
    # FTS
    if fts_path.exists():
        fts_path.unlink()
```
避免脚本里散落 `docker compose down && up -d` 的胶水.

Phase A (CPU, BGE_M3_GPU_ENABLED=false) → drain → wipe → Phase B (GPU, BGE_M3_GPU_ENABLED=true) → drain → 同 28 篇:
- 28 篇 = `_phase13b_common.discover_28_corpus(corpus_root)` (上面预检函数)
- 单篇: `build_notify_payload(doc_hash, output_path, callback_url)` → POST `/v1/ingestion/notify` → poll `/v1/ingestion/status` 90s timeout
- pace 2000ms; 记录 `notify_ms` + `terminal_ms` + `chunks_indexed`
- **日志 (建议)**: 每篇完成一行 `[doc_hash] chunks=N notify=Xms terminal=Yms status=success/failed`, 跑完打印 `phase13b_poc_summary.json` (timestamps, p50/p99, peak_mem, throughput)
- 汇总: chunks/sec、p50/p99、GPU mem peak (risk #6: 优先 `POST /v1/admin/gpu/memory-stats` 拿 `torch.cuda.max_memory_allocated()` 精确值, fallback `/metrics` 抓 `ekrs_gpu_memory_peak_bytes`)

预检: `docker exec deployment-rag-1 printenv BGE_M3_GPU_ENABLED` — 错值立刻 fail-fast。

验收 (exit 0 iff 全过, 阈值支持 env override):
- Phase B chunks 总数 ≥ 7787
- Phase B 最大单篇 ≤ 30s 端到端 (`$T5_PERF_OVERRIDE_LARGEST_DOC_S` 默认 30)
- Phase B 2298-chunk 篇 ≤ 5s
- Phase B `gpu_memory_peak_bytes ≤ 6 * 1024^3`
- Phase A/B failure_rate == 0

### T5.2 phase13b_equiv_check.py

**GT 文件 (risk #2)**: 新建 `deployment/phase12-recall-gt.json`, 格式 `{"<doc_hash>": {"<query>": recall_float}}`. T5.2 直接 `json.load` 解析, 不依赖 Markdown 报告格式. 若缺失, `exit 2` (eng-review fix #2). 同时保留 `parse_verification_md_fallback()` 从 `.md` 表格容错解析 (Phase 12 v10 当前报告格式), 主+fallback 双轨.

**GT pre-validate (eng-review fix #2)**: 启动时读 GT, 若 20 篇 sampled docs 任一缺 recall 标注 → `exit 2` 明确失败 (不做 doc-intrinsic 退化 — 那等价于恒真).

T5.1 跑完后: 抽 20 篇 × 5 查询 = 100 retrieval。每篇:
1. POST `/v1/constraints {query, top_k:10}` → 取 top-10 chunk_id 集合
2. Phase B → Phase A 重启 + wipe + 重灌 → 重跑 → Jaccard ≥ 0.99
3. recall@10 差 ≤ 1pp (ground truth 从 `deployment/phase12-v10-verification.md` 取)
4. 子样本 5 篇: 直接 Qdrant scroll 拉 `dense`+`sparse`, cosine ≥ 0.999, sparse top-K=20 Jaccard ≥ 0.95

cosine = `np.dot(a,b)` (向量已 L2-norm)。sparse Jaccard 前置 filter `_SPECIAL_TOKEN_IDS = frozenset({0,1,2,3,250001})` (跟 torch_bge_m3.py:62 对齐)。

### T5.3 phase13b_failover_test.py

**Dev endpoints (eng-review fix #3 + risk #6)**:
1. `POST /v1/admin/gpu/invalidate` (X-Admin-Key, 跟 `/v1/admin/embedding-cache/flush` 同 auth layer)
2. `POST /v1/admin/gpu/memory-stats` (同 auth, 返回 `{"peak_bytes": int, "allocated_bytes": int, "device": "cuda:0"}` — 走 `torch.cuda.max_memory_allocated(device)` 精确读, 避开 Prometheus multiproc 5s 延迟)

`/v1/admin/gpu/invalidate` handler:
```python
router = encoding_router.get_router()
with router._lock:
    router._state.registration_attempted = True
    router._state.last_self_check_pass = False  # force next probe to fail
return {"status": "invalidated", "next_probe_will": "transition_to_cpu"}
```
下次 5s probe 触发 → `_self_check→False` → gpu→cpu transition + audit emit. 不依赖 chmod / 文件系统 perms / root.

**Auth 检查 (risk #3)**: T5.3 启动时 `if not os.environ.get("ADMIN_API_KEY"): print("WARN: ADMIN_API_KEY 未设置, 跳过 T5.3"); sys.exit(0)`. CI 必须先 `export ADMIN_API_KEY=...`. 测试环境若 `EKRS_DEBUG=true`, admin route 接受空 key (dev-mode bypass, 跟 phase 5.5F 一致).

Phase B 跑着, 触发模拟故障:
1. `POST /v1/admin/gpu/invalidate` (admin auth) → 下次 probe 看到 `_self_check→False` → 状态机 gpu→cpu
2. `BGE_M3_GPU_PROBE_INTERVAL_S=5` (env override, CI 加速)
3. tail audit.log, grep `'"event": "channel_switched"' AND '"from_channel": "gpu"' AND '"to_channel": "cpu"'` (过滤 startup 的 unknown→gpu)
4. 测 `transition_detection_ms ≤ 30s`
5. `asyncio.gather` 10 个并发 notify → 全部 `status=success`, ≥1 个走 CPU (看 _run_step5 "channel=" 日志)

恢复: `chmod 644` 后再触发 probe, 验证 cpu→gpu 恢复 + 第二次 audit emit。

### T5.4 test_phase13b_t5_e2e.py (heavy)

```python
@pytest.mark.heavy
def test_phase13b_t5_full_e2e(tmp_path):
    bench = phase13b_poc_bench.run(corpus_root=P12_28DOC, phase="full")
    assert bench.phase_b.largest_doc_ms <= 30_000
    assert bench.phase_b.gpu_memory_peak_bytes <= 6 * 1024**3
    equiv = phase13b_equiv_check.run(sample_n=20, seed=42, corpus_root=P12_28DOC)
    assert equiv.mean_top10_jaccard >= 0.99
    assert equiv.mean_cosine >= 0.999
    assert equiv.mean_sparse_jaccard >= 0.95
    fo = phase13b_failover_test.run(probe_interval_s=5, concurrent_docs=10)
    assert fo.transition_detection_ms <= 30_000
    assert fo.all_succeeded and fo.at_least_one_cpu
```

CI: `pytest -m heavy tests/integration/test_phase13b_t5_e2e.py` (heavy 默认从 addopts 排除)。

### T5.5 test_phase13b_t5_acceptance.py (unit)

纯 Python 桩覆盖所有 10 个 §6 验收行 — 不需要真 GPU。三场景 (对应 Q5):
1. GPU 健康 → encode_gpu raise → state→cpu + audit 一次
2. GPU OOM RuntimeError → state→cpu + recovery (force_re_register_gpu True → state→gpu + 第二次 audit)
3. 10 并发 route() → 全部返 EncodedVector, audit 恰好 1 次 (transition-only, 不能再多)

其它行 (≤6GB / 7787≤30s / sparse / cosine / self_check) 用 stubbed encode_gpu + 计时 + 桩 EmbeddingService 对比。

### T5.6 提交

```
test(prod): Phase 13b E2E acceptance suite (28-doc Phase12-subset bench + equiv + failover)
```

无新 tag (T6 closure 留给 phase13b force-move)。

## Reuse (不要重写)

- `scripts/live_stress_60.py:511-548` read_corpus
- `scripts/live_stress_60.py:632-645` build_notify_payload
- `scripts/live_stress_60.py:653-689` notify_one (T5.1 thin-copy + 调优 pacing)
- `scripts/phase13a_t10_e2e.py:208-240` audit grep 模式
- `services/torch_bge_m3.py:62` _SPECIAL_TOKEN_IDS
- `services/encoding_router.py:201-211` force_re_register_gpu (T5.3 触发点)
- `services/encoding_pool.py:155-166` 30s 探针 (确认生产路径)
- `tests/integration/test_embedding_heavy.py` @pytest.mark.heavy 模板
- `tests/unit/test_phase13b_t3_probe.py` daemon-thread 桩模式

## Verification

1. `cd rag && pytest tests/unit/test_phase13b_t5_acceptance.py -v` — T5.5 全过
2. `cd rag && pytest tests/unit tests/golden_set -q` — 0 退化 (golden 208 通过, acceptance line #6)
3. `cd rag && mypy ekrs_rag/ scripts/ --config-file mypy.ini | grep -v "torch\|onnx"` — 干净
4. 容器 `BGE_M3_GPU_ENABLED=true`, `cd deployment && docker compose up -d`, 跑 `python scripts/phase13b_poc_bench.py --phase full --corpus-root /home/pangzy/code_project/doc-to-md/output/text` — exit 0
5. 接着跑 `phase13b_equiv_check.py --corpus-root ... --seed 42` — exit 0
6. 接着跑 `phase13b_failover_test.py --probe-interval-s 5` — exit 0
7. `cd rag && pytest -m heavy tests/integration/test_phase13b_t5_e2e.py -v` — pass

## Unresolved Questions

- **UQ-A**: 28 篇必须真实存在于 `/home/pangzy/code_project/doc-to-md/output/text/` (3809 dirs)。T5.1 第一次跑前, 先验证前 28 个 dir 都有非空 `data.jsonl` — 若有缺失需回退到 Phase 12 v10 verification 实际跑过的清单
- ~~**UQ-B**: T5.2 召回 ground truth 源 — `deployment/phase12-v10-verification.md` 列了 28 篇文件名但 recall 标注未必齐全; 缺则用 doc-intrinsic 信号 (chunks_indexed 当全集) 兜底, 接受更宽松阈值 (≤2pp 而非 ≤1pp)~~ — **RESOLVED** by eng-review fix #2 (T5.2 fail-fast exit 2 if GT missing)
- ~~**UQ-C**: 容器内 `chmod 000 /home/pangzy/code_project/bge-m3` 取决于 host 挂载是否保留 POSIX 权限; 若失败, T5.3 改用 `force_re_register_gpu` stub endpoint (需 dev mode hook, Phase 13c 议题)~~ — **RESOLVED** by eng-review fix #3 (POST /v1/admin/gpu/invalidate)
- ~~**UQ-D**: Phase B `gpu_memory_peak_bytes` 从 `/metrics` 抓取的 race — Prometheus multiproc 5s 刷新间隔可能让 bench 收不到峰值; 备选直接读 `torch.cuda.max_memory_allocated(device_id)` via debug endpoint~~ — **RESOLVED** by T5.1 immediate-scrape (read `/metrics` right after Phase B final status=success)
- **UQ-E**: `_emit_channel_switched` 在 pebble worker subprocess 里 silent drop 的风险 (audit writer 未注入) — T5.3 触发前先 `tail audit.log` 确认 worker emit 已在写

---

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | not run (test infra, no product scope) |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | skipped (not requested) |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | **CLEAR** | 6 architecture issues, 3 critical test gaps — all resolved with user-approved fixes |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | not run (backend-only, no UI) |

**UNRESOLVED:** 0 (UQ-A validation is pre-flight check at T5.1 startup; UQ-E is pre-flight `tail audit.log` check at T5.3 startup)
**VERDICT:** ENG CLEARED — ready to implement

### Eng Review Fixes Applied (per user-approved recommendations)

1. **T5.1 drain loop** — poll all 28 doc_hash via `/v1/ingestion/status` with 120s timeout before container restart, ensuring `success`/`failed` terminal state. Prevents `task_repo` re-enqueue race.
2. **T5.2 GT pre-validate** — read `deployment/phase12-v10-verification.md`; fail-fast exit 2 if any of 20 sampled docs lacks recall labels. No doc-intrinsic fallback (would be trivially-true).
3. **T5.3 admin endpoint** — new `POST /v1/admin/gpu/invalidate` (X-Admin-Key auth, sibling of `/v1/admin/embedding-cache/flush`). Handler sets `router._state.last_self_check_pass=False` so next 5s probe sees `_self_check→False`. Replaces fragile `chmod 000` approach.

### User Risk-Review Fixes Applied (6 risks + 3 suggestions)

1. **Risk #1 (28-doc pre-check)** — `discover_28_corpus()` filters by `(dir/'data.jsonl').exists() AND size>0`; fallback to `_phase13b_poc_28doc_fallback.txt` (Phase 12 v10 verification actual list)
2. **Risk #2 (GT format)** — `deployment/phase12-recall-gt.json` with `{doc_hash: {query: recall}}`; keep `.md` parser as fallback
3. **Risk #3 (admin auth)** — T5.3 skips with WARN if `ADMIN_API_KEY` unset; `EKRS_DEBUG=true` bypasses auth (Phase 5.5F dev-mode pattern)
4. **Risk #4 (perf line feasibility)** — smoke bench single biggest doc first; `$T5_PERF_OVERRIDE_LARGEST_DOC_S` env override (default 30s)
5. **Risk #5 (drain timeout)** — 120s → 300s; `--force` flag to bypass; per-status counter logging every 5s
6. **Risk #6 (GPU mem peak)** — new `POST /v1/admin/gpu/memory-stats` returns `torch.cuda.max_memory_allocated()` exact value; fallback to Prometheus `/metrics`
7. **Suggestion (wipe)** — `_phase13b_common.reset_state()` encapsulates Qdrant delete+recreate + FTS unlink
8. **Suggestion (logging)** — per-doc line `[doc_hash] chunks=N notify=Xms terminal=Yms status=...`; `phase13b_poc_summary.json` summary
9. **Suggestion (CI)** — `make t5-acceptance` marked `requires-gpu: true`; runs on dedicated GPU runner only, not PR gate

### CI Integration (建议)

`make t5-acceptance` 标记 `requires-gpu: true`. GitHub Actions / GitLab CI 在专用 GPU runner (`runs-on: [self-hosted, gpu, nvidia-4070]`) 手动触发, 不在常规 PR gate. 必传 `secrets.ADMIN_API_KEY` + `secrets.PARSER_TOKEN`. 本地无 GPU 开发机 `make t5-acceptance` 立即 exit 0 (T5.3 warn + skip, T5.5 跑 unit 桩).