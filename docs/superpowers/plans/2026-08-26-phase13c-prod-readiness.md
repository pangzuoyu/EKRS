# Phase 13c — GPU 通道 生产就绪化 (Prod Readiness)

> 上游: Phase 13b GPU bge-m3 PoC 闭 (43e81d9, version 0.5.0)。T5.1 28-doc bench 25/28 success, p99=8s, GPU util 83-100% spikes verified。
> 本阶段目标: 把"能跑"提升为"可观测、可运维、生产可用"。5 P0/P1 任务 + 1 P2 可选项。
> Tag discipline: post-13b incremental, **无新 tag** (跟 T10b-3 / T10d 模式一致, `phase13b` 锁 4d9523d)。

## Context

Phase 13b T3 ship 了 `_emit_channel_switched` 审计事件, T4 ship 了 GPU Prometheus 指标。两者都依赖"调用方能拿到 AuditWriter / 写入 PROMETHEUS_MULTIPROC_DIR"。

但编码跑在 Pebble **子进程**, AuditWriter + Prometheus multiproc **只主进程有**。
PoC 期间 4 个缺口被记录: (a) channel_switched 子进程静默丢; (b) `mark_process_dead(pid)` 未调用 → 多进程残留 stale counter; (c) `get_status` 把 failed 误报 pending (ingestion.py:599-606); (d) bench 阈值 7787 对 28-corpus (3618 chunks) 永远 fail。

T5.1 bench 用 `T5_DRAIN_TIMEOUT_S=1` 绕过了 (c)+(d), 但生产路径不可接受。

## Scope

**做 (P0/P1)**:
- T1: 跨进程 AuditWriter (Queue + consumer thread, parent §204 隔离)
- T2: prometheus_client `mark_process_dead(pid)` on worker shutdown + 验证 worker GPU 指标真聚合
- T3: get_status FAILED → "failed" 修复 (跟 queued/running/completed 三态对齐)
- T4: bench 阈值动态化 (corpus_total * 0.9)
- T5: `deployment/phase13c-ops-guide.md` (前置 + 部署 + 验收 + 排查 + 回滚)

**可选 (P2)**:
- ~~T6: channel_switched 抑制 (5min sliding window + count 字段)~~ → **DEFERRED** (2026-08-26 user, 见 T6 章节)

**不做**:
- 不改 Pebble 进程模型 (一 worker 一 router 是 invariant)
- 不引第三方 IPC 框架 (multiprocessing.Queue stdlib 够用)
- 不重写 AuditWriter (Phase 5.5 F 刚 ship 的 RebuildingRotatingFileHandler 留)
- 不动 production `EKRS_RATE_LIMIT` 默认值 (60/min, 仍是 default)
- T5.2/T5.3 真实-infra bench 仍是独立 follow-up (本计划不收)

## Files

**新 1**:
- `deployment/phase13c-ops-guide.md` (~150 LOC)

**改 6**:
- `rag/ekrs_rag/services/encoding_router.py` — `_emit_channel_switched` 走 Queue
- `rag/ekrs_rag/services/encoding_pool.py` — `_init_child` inject Queue addr + atexit `mark_process_dead`
- `rag/ekrs_rag/main.py` — lifespan 起 Manager().Queue() + consumer thread + 5min stale-cleanup 后台任务
- `rag/ekrs_rag/api/routes/ingestion.py:599-606` — FAILED 分支拆出
- `shared/ekrs_shared/models.py` — IngestionStatus.status Literal 扩 "failed" (T3 必需)
- `scripts/phase13b_poc_bench.py` — threshold 改为 `corpus_total * 0.9` (warning 默认; STRICT env 切 hard fail)

## Tasks

### T1: 跨进程 AuditWriter (P0, 1-2 天)

**走 multiprocessing.Queue + 后台 consumer thread** (用户推荐方案 A)。

Steps:
1. 新 `rag/ekrs_rag/observability/audit_bridge.py`:
   - `class AuditEventBridge` — Queue holder + drain thread, 在 main process lifespan 创建
   - 内部用 `multiprocessing.Manager().Queue(maxsize=10000)` (UQ-1 决议, Manager Queue 可跨 spawn 进程共享)
   - `put(event_name, **kwargs)` — serialize 到 dict, `queue.put_nowait(...)`, 满走 drop-oldest + `audit_events_dropped_total` Counter++ (UQ-2 决议, 不阻塞编码)
   - Consumer thread: `while not stop_event: queue.get(timeout=0.5) → writer.write(event, **payload)`, 全 try/except 隔离 (parent §204)
   - **分层容错** (D2 决议): `put()` 内 `queue.Full` / 序列化错 → debug log + counter++, **永不上抛** (runtime silent drop); Manager 启动失败 → lifespan raise (fail-loud)
2. `main.py` lifespan:
   - 启动 `Manager()` 重试 1 次 (D2 决议): `for attempt in range(2): try: manager = multiprocessing.Manager(); queue = manager.Queue(maxsize=10000); break; except Exception: log; time.sleep(1); ... 最后失败 raise`
   - 起 `AuditEventBridge(writer=_audit_writer)` + thread.start()
   - 暴露 `os.environ["EKRS_AUDIT_QUEUE_ADDR"]` (Manager queue 的 proxy ref, 子进程重建)
   - lifespan exit: `bridge.stop()` join thread, 5s grace (UQ-3 决议), 超时 WARNING + 强制 exit + **del os.environ["EKRS_AUDIT_QUEUE_ADDR"]`** (Section 2 note: 防 env leak 到下个测试 / 进程)
3. `encoding_pool.py:_init_child`:
   - item 6 (新): 读 `os.environ["EKRS_AUDIT_QUEUE_ADDR"]`, 重建 `multiprocessing.Manager().Queue()` proxy 引用, 注入模块级 `_audit_queue` 全局
   - 若 addr 缺失 (主进程没起 bridge) → debug log, worker 走 silent drop 旧行为
4. `encoding_router.py:_emit_channel_switched`:
   - `writer = get_writer(); if writer: writer.write(...)` (主进程快路径)
   - else: `from ..observability.audit_bridge import get_bridge; get_bridge().put(...)` (worker 慢路径)
   - 两路径都用 try/except, 失败 debug log 不抛
5. **不 backport** (UQ-8 决议): 只修 channel_switched, 不动 Phase 5.5 callback_failed 等主进程审计事件 (它们已有 AuditWriter 直接写)

Verify (D4 决议 — 双层测试):
- **单元 (mock)**: `AuditEventBridge.put + drain + write` round-trip, maxsize full → drop counter ++, writer raises → consumer 仍 alive
- **集成 (subprocess)**: `subprocess.run([sys.executable, "-c", "..."])` 起真 worker 跑 encode + channel_switched, 主进程 `bridge.drain(timeout=N)` + 断言 audit.log 文件含该事件. pytest mark `integration + heavy`, 跟 T5.1 pattern 一致 (避 Phase 13b sentencepiece 同款坑)
- GPU→CPU fallback smoke: 手动 `force_re_register_gpu()` 后 `audit.log | grep channel_switched` 命中

### T2: Prometheus Multiproc 完整化 (P0, 0.5 天)

`PROMETHEUS_MULTIPROC_DIR` env 已经在 `encoding_pool._init_child:76-78` 设过。**真正缺的是 `mark_process_dead(pid)`** —— worker 死掉后它的 counter 文件留尸, sidecar 会读到 stale 巨大值。

Steps:
1. `encoding_pool.py` 新增 `_worker_exit_hook`:
   - `import atexit; atexit.register(lambda: prometheus_client.multiprocess.mark_process_dead(os.getpid()))`
   - `_init_child` 末尾调用注册 (处理 graceful shutdown / SIGTERM)
2. `main.py` lifespan:
   - **stale cleanup 后台任务** (UQ-4 + D3 决议): `asyncio.create_task(_cleanup_stale_prometheus_files())`, 每 5min **wrap 进 `asyncio.to_thread`** 避免阻塞 event loop, 扫 `PROMETHEUS_MULTIPROC_DIR/*.db`, 文件名 `<pid>_<type>.db`, `os.kill(pid, 0)` 探测, **mtime < time.time() - 60 才认为 stale** (避免误删活跃 worker 的 .db), 死的 pid 调 `mark_process_dead(pid)` + unlink
   - lifespan exit: 收集 worker pid (`pebble.Pool` API 暴露) + 兜底 `mark_process_dead`
3. Verify: worker process restart 后 `du -sh $PROMETHEUS_MULTIPROC_DIR` 不持续增长; `curl /metrics | grep gpu_memory_peak_bytes` 显 worker 真实数据 (非 stale 大值)

Verify (D3 决议):
- 单元测试: (a) `mark_process_dead` 调用后 `.db` 文件被 cleanup (test_atexit fixture); (b) mtime 旧文件清理, 新文件保留 (race test, monkeypatch time); (c) asyncio.to_thread wrap 验证 (mock 阻塞函数, 验 event loop 不卡); (d) `os.kill(pid, 0)` 对死 pid 返 False
- 集成测试: 启 5 worker, kill 3, /metrics 仍聚合活的 2 个
- 端到端: GPU bench 完成后 `audit.log + /metrics | grep gpu_memory` 都可见

### T3: get_status pending bug 修复 (P1, 0.5 天)

`ingestion.py:599-606` 当前: `elif status in ("failed", "pending"):` 一锅出 "pending"。

Steps:
1. **改模型为 Literal 4 值** (D1 决议): `shared/ekrs_shared/models.py` `IngestionStatus.status: Literal["pending","processing","success","failed"]` (替换原 `str` + 注释 `processing|success|failed`; 模型 + 注释 + 测试契约三方对齐 — `test_notify_step5_wiring.py:376` 已接受 pending/processing)
2. **新映射函数** `rag/ekrs_rag/services/ingestion_mapper.py` (or `shared/ekrs_shared/mapping.py`):
   ```python
   def map_row_status_to_ingestion_status(row_status: str) -> Literal["pending","processing","success","failed"]:
       return {"queued":"pending", "running":"processing", "pending":"pending",
               "failed":"failed", "completed":"success"}.get(row_status, "failed")
   ```
3. 拆 `ingestion.py:599-606` 三分支 + 用映射函数:
   ```python
   elif row_status in ("queued", "running", "pending"):
       return IngestionStatus(..., status=map_row_status_to_ingestion_status(row_status))
   elif row_status == "failed":
       return IngestionStatus(..., status="failed", failure_reason=row.get("failure_reason", "unknown"))
   ```
4. 检查 TIMEOUT 状态映射 (IngestionOutcome enum + get_status 全路径) — 防止类似 bug 漏掉

Verify:
- 单元测试: (a) 映射函数 5 路全覆盖, (b) Literal enum 校验拒绝外来值, (c) get_status 返 "failed" 不再 "pending"
- 集成测试: 提交 empty JSONL (故意 no_chunks) → /v1/ingestion/status 返 "failed", bench drain 立即退出不等 timeout (D2-user added)
- 回归: golden_set 50 case (50 case 是 success/pending 路径, 跟新 Literal 兼容)

### T4: Bench 阈值动态化 (P1, 0.5 天)

`phase13b_poc_bench.py:279` 默认 7787 (用户描述 7000, 实际代码 7787) — 28-corpus 总 3618 chunks 永远 fail。

Steps:
1. bench 启动时 (main 入口): `corpus_total_blocks = sum(len(get_blocks(jsonl_path)) for _, jsonl_path in corpus)` (D2-user added, ~28 文件 <1s 开销)
2. `T5_PHASE_B_MIN_CHUNKS` 优先级 (UQ-9 决议, **warning 而非 hard fail**):
   - `T5_PHASE_B_MIN_CHUNKS=0` → 绝对关闭
   - explicit env (正整数) → 严格阈值
   - `int(corpus_total_blocks * 0.9)` 默认 → **warning log 但 exit 0** (验证 GPU 路径, 不验证 chunker)
3. 新 env `T5_PHASE_B_MIN_CHUNKS_STRICT` (default false):
   - true 时低于阈值 → hard fail (生产预发布 gate 用)
   - false 时低于阈值 → WARNING + exit 0 (CI / 验证用)
4. stdout + report JSON 写出 `corpus_total_blocks`, `threshold_used`, `threshold_pct_of_corpus`, `threshold_status` ("pass" | "warn" | "fail") (debug 透明度)

Verify:
- 单元测试: env override / corpus-derived / disabled / STRICT 四路径 (4 case)
- 集成: (a) bench 28-corpus 跑通, `threshold_used = 3258 (corpus_total_blocks * 0.9)`, exit 0, 不再误报; (b) 提交故意失败 doc (空 JSONL), bench drain 立即退出不等 timeout (D2-user added, T3 → T4 联动)
- STRICT=true 时低于阈值 → exit non-zero

### T5: Ops 部署清单 (P1, 0.5-1 天)

`deployment/phase13c-ops-guide.md` 内容大纲:
1. **前置条件**: NVIDIA driver ≥ 535, CUDA 13, docker compose v3.8+, nvidia-container-toolkit
2. **构建**: `cd deployment && docker compose --profile gpu build rag-gpu` (~5min, 3GB image)
3. **启动**: `make gpu-up` (precheck host weights, stop CPU rag, start rag-gpu, healthcheck)
4. **验收**: `make gpu-acceptance` (Phase 13b T5.1 28-doc bench), 输出解读:
   - `failure_rate == 0` 且 `chunks >= threshold` → ship
   - `gpu_memory_peak_bytes < 6GB` → 健康
   - audit.log 出 `channel_switched` 事件 → 路径通了 (T1 验收)
   - `/metrics` 聚合 worker GPU 指标 → T2 通了
5. **常见故障**:
   - `sentencepiece not installed` → 重建 image (43e81d9 已修)
   - `min cosine below threshold` → 检查 probes fixture
   - `gpu_memory_used 一直 0` → nvidia-smi 1s polling (5s 错过 spike)
   - `notify duplicate` → `_phase13b_common.build_notify_payload` 已用 `int(time.time())` 默认
6. **回滚**: `make gpu-down` (恢复 CPU rag 服务, profile gpu down 不删 volume)
7. **升级路径**: 13b → 13c → 13d (后续) 的 changelog 引用

Verify:
- 同操作员 +30min SLA: ops 手册读者按步骤能 0-疑问启 GPU 服务
- `make gpu-down` 真恢复 CPU rag (Phase 11 closure healthcheck fix 仍生效)

### T6 ~~(P2 可选): channel_switched 抑制~~ → DEFERRED

**Status: DEFERRED (2026-08-26 user decision)** — Phase 13c 不包含 T6, 审计事件不抑制。如果未来产生噪声, 在 Phase 14 或单独补丁中处理。

~~GPU 健康状态抖动 (self_check 周期 pass/fail/pass) 会反复触发 channel_switched 事件, 噪声污染 audit.log。~~

~~Steps (本计划留待 user 决定要不要做):~~
~~1. `_emit_channel_switched` 内存 sliding window (5min TTL):~~
   ~~- key = `(from_channel, to_channel, reason)`~~
   ~~- 滑窗用 `functools.lru_cache(maxsize=128)` 装饰 5min-bucketed `make_key(now_ts)` (128 entries 覆盖 ~10h @ 5min bucket, 防内存泄漏)~~
   ~~- window 内重复 → `bridge.put(event_name, ..., suppressed_count=N)` (累计 +1)~~
~~2. 真实 fallback (e.g. self_check 失败 5min + 真 retry succeed) 仍出独立事件~~

~~Verify: (a) monkeypatch time.sleep; 触发 100 次同 key → audit.log 1 条 + count=99; (b) lru_cache 命中/失效边界 (手动 `cache_clear()`); (c) 跨 key 独立计数 (10 次 keyA + 10 次 keyB → 2 条 audit.log 各自 count=9)~~

**Why deferred**:
- 自检稳态时 channel_switched 实际频率低 (T5.1 bench 28 篇只见 1-2 次)
- LRU sliding window 本身有维护成本 (cache_clear 时机、跨进程一致性)
- Phase 13c P0/P1 风险点更优先 (audit gap / stale counter / status 误报)
- 噪声触发后再单独评估, 避免 speculative implementation

## TDD 顺序

```
T3 (独立, 0.5d) ─┐
T4 (独立, 0.5d) ─┼─ (并行, 三任务不同 file) ─→ T5 (1d, 收尾文档)
T1 (1.5d) ───────┤
T2 (0.5d) ───────┘ (T2 可与 T1 并行; 都改 encoding_pool.py + main.py)
T6 (0.5d, optional, 跟 T1 走, 不影响主线)
```

T1/T2/T3/T4 互相独立, 可 4 人并行 / 单人 sequential 顺序 T3→T1→T2→T4→T5。
T5 收尾文档依赖前面 4 个全部闭 (写真实功能状态)。
T6 P2 可选, 跟 T1 同步走 (复用 bridge 接口), 不影响主线 ship。

## Verification 套件

- 单元: T1 +5 (bridge round-trip/drop/exception isolation/cross-process mock/cleanup), T2 +3 (atexit hook/cleanup/multiproc dir), T3 +2 (failed status mapping/TIMEOUT mapping), T4 +3 (env override/corpus-derived/disabled)
- 集成: T1 +1 (worker → bridge → audit.log 真路径), T2 +1 (worker restart stale cleanup), T3 +1 (空 JSONL 端到端), T4 +1 (bench 真跑不误报)
- 回归: golden 50 case, Phase 13b 633 unit + 208 golden + 11 t10b3 = 852 baseline pass 0 退化
- mypy: 全 rag/ + scripts/ clean

## Follow-up (本计划不收)

- **T5.2** (检索等价): `phase13b_equiv_check.py` 真比对 GPU vs CPU top-10, GT JSON `deployment/phase12-recall-gt.json` 已就位
- **T5.3** (故障转移): `phase13b_failover_test.py` 跑 GPU probe 触发 fallback + nack 注入
- **13d+**: 内嵌 FP32 weights (~5GB image), shippable 模式 (当前 bind-mount 是 PoC)
- **Phase 14**: cross-region audit log shipping (合规需求)

## 不做 (out of scope)

- 不动 Pebble 进程模型 (1 worker 1 router)
- 不引跨 IPC 框架 (ZMQ / gRPC)
- 不改 AuditWriter / AuditIndex (Phase 5.5 F 刚 ship)
- 不动 production `EKRS_RATE_LIMIT` 默认 60/min
- 不动 golden_set (50 case 不增不减)
- 不写 distributed lock (AuditWriter 单写者, Queue + 单 consumer 足够)

## Reuse

- `multiprocessing.Queue` stdlib — 无新 dep
- `prometheus_client.multiprocess.mark_process_dead` — 已在用, 只补 hook
- Phase 5.5 F `RebuildingRotatingFileHandler` — audit.log 100MB × 5 gzip 仍生效
- `_phase13b_common.build_notify_payload` — T4 验收用, 不动
- `make gpu-up / gpu-down / gpu-acceptance` — T5 ops 指南直接引用

## UQ (Decisions)

| UQ | 主题 | 决议 |
|---|---|---|
| UQ-1 | Queue 跨进程传递机制 | `multiprocessing.Manager().Queue()`, env var (`EKRS_AUDIT_QUEUE_ADDR`) 传 Manager proxy ref |
| UQ-2 | Queue maxsize + drop 策略 | 10000 + drop-oldest; Prometheus counter `audit_events_dropped_total` 暴露 (不阻塞编码) |
| UQ-3 | Consumer thread shutdown timeout | 5s grace; 超时 WARNING + 强制 exit (不阻塞服务停止) |
| UQ-4 | `mark_process_dead` SIGKILL 兜底 | main lifespan 起 5min 后台任务, 扫 multiproc_dir + `os.kill(pid, 0)` 探测 + `mark_process_dead` 清理 |
| UQ-5 | IngestionStatus Literal 扩 "failed" | **必修**, `shared/ekrs_shared/models.py` Literal 扩为 `"success" \| "pending" \| "failed"` |
| UQ-6 | T5 ops 文档多语言 | 中文 (跟 CLAUDE.md 一致); 英文版留 follow-up |
| UQ-7 | T6 抑制策略 sliding window TTL | 5min (GPU↔CPU fallback 通常 < 1/h, 5min 足够降噪) |
| UQ-8 | T1 backport Phase 5.5 事件 | **不 backport**, 范围隔离: 只修 worker 子进程 channel_switched; 主进程 callback_failed 等已有 AuditWriter |
| UQ-9 | T4 阈值违规处理 | **warning 默认** (exit 0), `T5_PHASE_B_MIN_CHUNKS_STRICT=true` 切 hard fail |

## Memory update (任务完成后)

写 `phase13c-prod-readiness.md`:
- commit hash + 5 tasks ship 时间线
- UQ-1 ~ UQ-7 决议 + 理由
- T6 ship/skip 二选一 user 决定
- 跟 phase13b-gpu-poc-verified 链接 (T1 修了 audit gap = 闭环 13b 留的洞)
- Bench 跑通 base 数据: chunks ≥ 3258 (corpus_total * 0.9), GPU mem peak ≤ 6GB

## 建议 commit 顺序 (user 审阅给出)

```
1. T3 (状态修复 + Literal 扩) — 最简单, 独立文件, 单独 commit
2. T4 (bench 阈值动态化)      — 独立文件, 单独 commit
3. T1 + T2 (合并一 commit)    — 都改 encoding_pool + main, 跨进程基础设施一起 ship
4. T5 (ops 文档)              — 收尾 commit, 引用上面 3 commit 的真实状态
```

理由: T3 风险最低先合, 给后续 commit 兜底 (回归测试基线); T1+T2 合并因为两者都涉及子进程环境管理 (env var 传递、生命周期 hook), 拆开 ship 会让中间状态不完整; T4 跟 T1+T2 不冲突可单独; T5 文档依赖前面闭。

## 不写 commits / push (per "no commits unless asked")

## GSTACK REVIEW REPORT

`gstack-plan-eng-review` skill 完整执行 (Step 0 preamble + Section 1-4 + outside voice + summary), 输出 4 个决策 brief (D1-D4), user 全部拍板。0 unresolved。

| # | 决策 brief | 决议 | 落地点 |
|---|---|---|---|
| **D1** | IngestionStatus.status 字段是 `str` 不是 `Literal`, 但测试接受 4 值 (pending/queued/running/processing) + benchmark 实测需要 "failed" 准确语义 | **A**: Literal 4 值 + 新增 `map_row_status_to_ingestion_status` 映射函数 | `shared/ekrs_shared/models.py` `Literal["pending","processing","success","failed"]` + `services/ingestion_mapper.py` (5-path coverage) |
| **D2** | T1 跨进程 AuditWriter 启动失败容错粒度 | **B**: 分层容错 (Manager retry once + 运行时 best-effort, 启动失败 → fail-loud) | `main.py` lifespan retry 1 次 + 5s sleep, `bridge.put()` 异常隔离 (debug log + counter++), `del os.environ["EKRS_AUDIT_QUEUE_ADDR"]` 在 lifespan exit 防 env leak |
| **D3** | T2 stale 清理 vs active worker race | **A**: `asyncio.to_thread` + mtime 检查 (< time.time() - 60 才 stale) | `services/stale_cleanup.py` 用 to_thread 跑文件 scan; `os.kill(pid, 0)` 验证 liveness |
| **D4** | T1 跨进程测试策略 | **B**: 双层 (单元 mock + 集成 subprocess) | unit: 12 case (Manager/Queue bridge + 异常隔离); integration: `subprocess.Popen` 起 worker + 真 Queue + 真 audit.log 写入验证 |

**4 architectural risks** identified and resolved — 不留未决议题。

**User-added refinements** (eng-review 之外的 2 项 user 拍板):
- T4: `corpus_total_blocks = sum(len(get_blocks(jsonl)) for _, jsonl in corpus)` 在 bench `main()` 启动时算 (28 文件 <1s 开销)
- T4 → T3 集成验证: bench drain 遇 failed status 立即 exit 不等 120s timeout

**T6 deferred decision** (2026-08-26 user): Phase 13c 不收 channel_switched 抑制。噪声若真实产生, 留 Phase 14 或单独补丁。

**Next Steps** (per skill "Review Chaining"):
- All relevant reviews complete → plan ready for execution
- Suggested start: TDD cycle on T3 (per commit order: T3 → T4 → T1+T2 → T5)
- T6 P2 **deferred** (噪声未真产生, 留 future phase)
- 执行前可跑 `/qa` 跑 plan-level drift check, 或 `/ship` 当所有 commit landed 后
- 当前状态: **CLEAN, plan ready** (0 unresolved, 4 issues resolved, T6 deferred)

写 plan 不开 commits, 等 user 决定 commit 节奏。