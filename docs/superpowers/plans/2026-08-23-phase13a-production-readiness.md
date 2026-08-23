# Phase 13a — RAG 生产就绪化(P0 主线)Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 v10 验证过的数据面装进生产控制面 — encode 出 event loop、健康检查解耦、分层超时、实数准入;CPU-only 即生产可用,GPU(P13b/GPU spec)后接。

**Architecture:** notify 路由内联做 Steps 1-4(读 JSONL+chunk+准入,毫秒级);Step 5 全段(encode→Qdrant→FTS→清旧版)交 **pebble spawn 进程池**(每 worker 独立加载 bge-m3);父进程 `wait_for(1800s)` 超时 SIGKILL;status 保持 doc_hash 键控增 queued/running 态。

**Tech Stack:** pebble 5.2.1(aliyun 镜像已验证)/ FastAPI 现栈 / aiosqlite TaskRepo / prometheus_client multiproc

**Spec:** `docs/specs/phase13-rag-production-readiness-spec.md`(v1.0,含 §0 勘误 E1-E10 与 §5 不变量清单 — **执行每个 task 前必读 §5**)

## Global Constraints(spec verbatim)

- R1-R8 七铁律不变;`IngestionOutcome` frozen 契约与路由 outcome→TaskRepo 映射不动
- status 端点 doc_hash 键控不变(doc-to-md 契约);notify 仍 202 秒回
- 新审计事件走 4 步: `_EVENT_SCHEMAS` 注册 + write-site + ekrs-handbook §16 inventory + 真实 AuditWriter 回归测试
- worker = pipeline Step 5 **全段**(含 FTS 配对写 E8 + delete_old_versions Range(lt) + 幂等键 skip + RedisLock)
- 准入: 入口粗筛 raw_chars ≤1M + **chunk 后实数硬闸 chunks ≤3000**(P0-4 拍板值)
- 超时: 全任务 1800s + 子批内 60s(P0-3 定稿);pebble spawn(禁 fork,E7)
- golden 208 零回归;full unit suite 零回归;mypy clean
- 测试跑法: `bash -c 'export PYTHONPATH=/home/pangzy/code_project/EKRS; cd rag && python -m pytest tests/unit -q'`(rtk 剥 inline PYTHONPATH)

## File Structure

```
rag/ekrs_rag/services/__init__.py          (新包)
rag/ekrs_rag/services/admission.py         准入双闸(粗筛+实数)
rag/ekrs_rag/services/step5_worker.py      可 pickle 的模块级 worker fn(spawn 子进程入口)
rag/ekrs_rag/services/encoding_pool.py     EncodingWorker: pebble 池 + submit + wait_for + kill
rag/ekrs_rag/api/routes/health.py          /ready(新); /healthz 瘦身
rag/ekrs_rag/api/routes/notify.py          改: Steps1-4 内联 + submit
rag/ekrs_rag/api/routes/status.py          增 queued/running 态
rag/ekrs_rag/observability/metrics_extra.py 3 个新指标
rag/ekrs_rag/observability/recovery.py    重启 running→pending
rag/ekrs_rag/main.py                       lifespan: 池启停 + recovery 挂载
shared/ekrs_shared/models.py               不动(复用 Chunk/IngestionNotification)
```

**核心接口(跨 task 契约,类型以此为准):**
```python
# admission.py
class AdmissionVerdict(TypedDict): ok: bool; reason: str; actual_chunks: int
def coarse_gate(notification: IngestionNotification) -> AdmissionVerdict      # raw_chars, ≤1_000_000
def chunk_gate(n_chunks: int) -> AdmissionVerdict                            # ≤3000
# step5_worker.py
@dataclass
class Step5Payload: doc_hash: str; version: int; trace_id: str; output_path: str
def run_step5(p: Step5Payload) -> dict   # 返回 IngestionOutcome 字段 dict(rag_status/chunks_indexed/error_code/error_message);幂等 skip 在内
# encoding_pool.py
class EncodingWorker:
    def __init__(self, max_workers: int = 2, task_timeout_s: float = 1800.0) -> None
    async def submit(self, p: Step5Payload) -> str            # task_id, 立即返回
    async def wait(self, task_id: str) -> dict                # 终态 outcome dict; timeout→{"rag_status":"failed","error_code":"task_timeout"}
    def stop(self) -> None
```

---

### Task 1: /ready 端点 + /healthz 瘦身

**Files:** Create `rag/ekrs_rag/api/routes/health.py`; Modify `rag/ekrs_rag/main.py`(路由注册); Test `rag/tests/unit/test_health_ready.py`

- [ ] **1.1 失败测试**:`/healthz` 只返 `{"status":"ok","uptime_s":N}` 不触 DB/依赖;`/ready` 在 qdrant ping OK+redis ping OK 时 200 `{"status":"ready"}`,任一挂时 503 `{"detail":"dependency unavailable"}`。mock `QdrantManager`/redis(用现有 `get_retriever` Depends 风格,Phase 5.5 E)。测试: `TestClient` + `_sync_lifespan`(cerebrum 已知坑: lifespan async-only)
- [ ] **1.2 跑失败** → FAIL(路由不存在)
- [ ] **1.3 实现** health.py(两个 GET;uptime 用模块级 `_START=time.time()`;/ready 里 qdrant 用 `count_points` 短路探活 + redis `PING`,两个都 try/except → 503)
- [ ] **1.4 跑过** → PASS
- [ ] **1.5 回归**: full unit + golden 208
- [ ] **1.6 Commit**: `feat(health): /ready dependency probe + slim /healthz (P0-1)`

### Task 2: 准入双闸 admission.py

**Files:** Create `rag/ekrs_rag/services/admission.py` + `__init__.py`; Test `rag/tests/unit/test_admission.py`

- [ ] **2.1 失败测试**(真实代码,非伪码):
```python
def test_coarse_gate_over_raw():
    n = IngestionNotification(trace_id="t", doc_hash="d", version=2, output_path="/x", callback_url="")
    # coarse_gate 读 output_path 下 data.jsonl 统计 raw chars(复用 pick_bundles 的 len(content.raw) 累加模式); >1_000_000 → ok=False reason="raw_chars_over_limit"
def test_chunk_gate_3000():
    assert chunk_gate(3000)["ok"] and not chunk_gate(3001)["ok"]   # 拍板值边界
def test_missing_jsonl_rejects(): ...  # 保守拒绝, ok=False reason="jsonl_unreadable"
```
- [ ] **2.2 跑失败** → FAIL
- [ ] **2.3 实现**(≤60 行;读文件统计,异常→保守拒绝)
- [ ] **2.4 跑过** → PASS;full unit 回归
- [ ] **2.5 Commit**: `feat(admission): coarse+chunk gates (P0-4, 实数硬闸 3000)`

### Task 3: step5_worker — 可 pickle 的子进程入口

**Files:** Create `rag/ekrs_rag/services/step5_worker.py`; Test `rag/tests/unit/test_step5_worker.py`

**Consumes:** pipeline 现有 Steps(读 pipeline.ingest 抽出的可复用段 — **不改 pipeline.ingest 本体**,从其 Step5 段复制逻辑成独立 fn,保持行为一致)

- [ ] **3.1 失败测试**(dummy embedding 模式跑真逻辑,mock Qdrant/FTS):
```python
def test_run_step5_happy(tmp_path, ...):   # 写 2-block JSONL → outcome rag_status=="success", chunks_indexed==N
def test_run_step5_idempotent_skip(...):   # Qdrant 已有同 doc+version → outcome skip, 不 encode
def test_run_step5_fts_paired_write(...):  # FTS replace_doc 被调用且 count 一致(E8)
def test_run_step5_rejects_over_gate(...): # 3001 chunks → rejected, 不 encode(与 Task2 闸一致)
```
- [ ] **3.2 跑失败** → FAIL
- [ ] **3.3 实现**:
```python
def run_step5(p: Step5Payload) -> dict:
    return asyncio.run(_step5_async(p))    # 子进程私有 loop;httpx/qdrant 在子进程内新建(禁跨进程传递)
async def _step5_async(p): # parse→chunk(快)→chunk_gate→幂等 skip→RedisLock→encode→qdrant.upsert→fts.replace_doc→delete_old_versions(Range lt)→outcome dict
```
  - 子进程内组件自建: QdrantManager/FTSManager/EmbeddingService 从 Settings 构造(spawn 干净态)
  - 锁: RedisLock 包裹全程,拿不到→outcome `concurrent_skip`(幂等键 md5(trace|hash|version) 语义照抄 pipeline)
- [ ] **3.4 跑过** → PASS;mypy
- [ ] **3.5 Commit**: `feat(worker): picklable Step5 worker fn with paired FTS write + idempotency`

### Task 4: EncodingPool — pebble 池 + 分层超时

**Files:** Create `rag/ekrs_rag/services/encoding_pool.py`; Modify `rag/pyproject.toml`(+`pebble>=5.0,<6`); Test `rag/tests/unit/test_encoding_pool.py`

- [ ] **4.1 失败测试**(真 pebble,轻 fn):
```python
def test_submit_returns_fast():            # submit 0.5s 内返回(task_id)
def test_wait_success():                   # fn=identity → outcome 透传
def test_wait_timeout_kills(monkeypatch):  # pool.schedule(timeout=) 触发 → wait 返回 task_timeout outcome;子进程确认死亡(pebble future.result() raises ProcessExpired → 捕获转 dict)
def test_pool_stop_drains():               # stop() 幂等
```
- [ ] **4.2 跑失败** → FAIL(pebble 未装则先 `uv pip install pebble`)
- [ ] **4.3 实现**:
```python
class EncodingWorker:
    def __init__(self, max_workers=2, task_timeout_s=1800.0):
        self._pool = pebble.ProcessPool(max_workers=max_workers, initializer=_init_child)
    async def submit(self, p) -> str:
        fut = self._pool.schedule(run_step5, args=(p,), timeout=self._task_timeout_s)
        self._tasks[task_id] = fut; return task_id
    async def wait(self, task_id) -> dict:  # await asyncio.wrap_future / to_thread(fut.result) + ProcessExpired→task_timeout
def _init_child():  # spawn initializer: 预加载模型/静默 httpx trace 日志
```
- [ ] **4.4 跑过**;真实池冒烟: submit 一个 run_step5 真 payload(小 doc)全链路 PASS
- [ ] **4.5 Commit**: `feat(pool): pebble spawn pool with 1800s kill + task registry (P0-2/P0-3)`

### Task 5: notify 路由重接 + status 新态

**Files:** Modify `rag/ekrs_rag/api/routes/notify.py`(或现 ingestion 路由文件)、`status.py`; Modify `main.py`(Depends 注入 EncodingWorker); Test `rag/tests/unit/test_notify_step5_wiring.py`

- [ ] **5.1 失败测试**:
```python
def test_notify_still_202_fast():          # Steps1-4 内联(含双闸) + submit → 202 <200ms(mock worker.submit)
def test_notify_admission_rejected():      # 粗筛/实闸超限 → 202 + outcome rejected(不裸 403, E10)+ 审计事件
def test_status_exposes_queued_running():  # TaskRepo 行态 pending→queued→running→terminal
def test_outcome_mapping_unchanged():      # 终态→TaskRepo COMPLETED/FAILED 映射与 Phase 6A 表一致
```
- [ ] **5.2 跑失败** → FAIL
- [ ] **5.3 实现**: handler 顺序 = coarse_gate → parse+chunk(loop 内,毫秒) → chunk_gate → TaskRepo queued 行 → worker.submit → 202;worker.wait 完成回调里写终态 + **callback(复用 `_send_callback_safely` 语义: 4xx 不重试/5xx 重试)**。Steps1-4 抽成 `_prepare_step5(notification)` helper(单一真源,pipeline.ingest 老路径保留给 replay)
- [ ] **5.4 跑过**;integration: 真 notify→pool→mock-qdrant 全链;**关键验收测试** `test_healthz_during_encode_under_100ms`(pool 跑慢 fn 期间打 /healthz 计时)
- [ ] **5.5 Commit**: `feat(route): Steps1-4 inline + async Step5 via pool; status queued/running`

### Task 6: 审计事件 4 步

**Files:** Modify `rag/ekrs_rag/main.py` `_EVENT_SCHEMAS` + step5_worker/notify write-site + `ekrs-handbook.md` §16; Test `rag/tests/unit/test_audit_phase13_events.py`

- [ ] **6.1** 事件: `admission_rejected{doc_hash, reason, actual_chunks}` + `task_timeout_killed{doc_hash, task_id, timeout_s}`(+count 22→24, handbook inventory 同步)
- [ ] **6.2** 失败测试: 真实 AuditWriter 断言两事件落盘(cerebrum 4 步 checklist 逐条)
- [ ] **6.3** 实现 + 跑过 + golden 回归
- [ ] **6.4 Commit**: `feat(audit): admission_rejected + task_timeout_killed (4-step)`

### Task 7: 指标 + 重启恢复

**Files:** Create `rag/ekrs_rag/observability/metrics_extra.py` + `recovery.py`; Modify `main.py` lifespan; Test ×2

- [ ] **7.1** `rag_task_queue_depth`(Gauge)/`rag_task_duration_seconds`(Histogram, 桶 [10,30,60,120,300,600,1800], label result)/`rag_doc_rejections_total`(Counter, label reason);multiproc 共享目录经现有 :9090 sidecar(Phase 5.5 D)汇聚 — **注意**: 池子进程也写指标时 initializer 里设 `PROMETHEUS_MULTIPROC_DIR`
- [ ] **7.2** recovery: 启动时 TaskRepo `UPDATE tasks SET status='pending' WHERE status IN ('queued','running')`(重启丢内存队列的兜底;pending 语义=等 re-notify, 幂等键保证安全重放)
- [ ] **7.3** TDD 全程;Commit: `feat(obs): queue/duration/rejection metrics + boot recovery (P1-1/2/3)`

### Task 8: P1-4 查询侧 encode 出 loop + P1-5 callback 对账日志

**Files:** Modify `rag/ekrs_rag/retrieval/qdrant_client.py`(query 路径 `asyncio.to_thread(self._embedding_service.encode, [q])[0]`); Create callback 失败日志(`logs/callback_failures.log` 走既有 RebuildingRotatingFileHandler 规约, **非 /tmp**); Test ×2

- [ ] **8.1** TDD: query encode 不阻塞 loop(to_thread 化后 `_ StubRetriever` 兼容性照 T10a-4 先例);callback 失败写对账文件(结构化行: ts/doc_hash/reason)
- [ ] **8.2** Commit: `feat(retrieval): query encode via to_thread; callback failure reconciliation log (P1-4/5)`

### Task 9: 13c 集成接口预留(GPU 插槽)

**Files:** Modify `rag/ekrs_rag/services/step5_worker.py`(encode 调用点抽 `_encode_backend(texts)` fn); Test 断言插槽纯函数可替换

- [ ] **9.1** 仅抽 fn + 注释指向 GPU spec G7;**不引 GPU 代码**(YAGNI, 13b 并行另立计划 `2026-08-23-phase13b-gpu-container.md` — 按 GPU spec §3/§9 写,torch FP16 优先)
- [ ] **9.2** Commit: `refactor(worker): encode backend seam for GPU channel (13c hook)`

### Task 10: 端到端验收 + 灰度门

- [ ] **10.1** E2E(真容器): 用 v10 真实 bundle 子集(含 1343-chunk OCR doc + 2298-chunk doc)走新链路;断言: /healthz 编码期 P99<100ms / 7787-chunk 拒绝+审计 / kill -9 子进程后池自愈 / golden 208 + full unit 零回归
- [ ] **10.2** staging 灰度: v10 同数据重跑对比 Qdrant/FTS count 一致(drift 检测器静默)
- [ ] **10.3** 13d 门(GPU 上线时): 10% 流量 → 检索等价验收(GPU spec §8 十项)→ 100%;CPU-only 已满足生产即回退底座

---

## Self-Review

- 覆盖: P0-1(T1)/P0-2(T3-5)/P0-3(T4)/P0-4(T2)/P1-1..3(T7)/P1-4/5(T8)/13c 钩(T9)/13d 门(T10) ✓ spec §5 不变量进 Global Constraints ✓
- 类型一致: AdmissionVerdict/Step5Payload/EncodingWorker 签名在 T2/T3/T4/T5 间已对齐 ✓
- 风险: T3 与 pipeline.ingest 的逻辑复制漂移 → 靠 T5.3 helper 单一真源 + T10.2 一致性对账兜底
