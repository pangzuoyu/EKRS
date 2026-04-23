工程知识恢复系统 (EKRS) 分阶段详细实施指南
阶段 1：解析系统数据库与原子信号（基础底座）
1.1 模块与文件结构
text
parser_system/
├── db/
│   ├── database.py          # 异步 SQLite 初始化
│   ├── models.py            # ParseTask SQLAlchemy 模型
│   └── task_repository.py   # CRUD 操作
├── pipeline/
│   └── orchestrator.py      # 修改 process_file 集成生命周期
├── rag/
│   ├── client.py            # RAG 通知客户端
│   └── callback_server.py   # FastAPI 回调接收（占位）
├── services/
│   ├── heartbeat.py         # 僵尸任务监控
│   └── version_cleanup.py   # 版本目录清理
├── config.py                # 新增环境变量
└── tests/
    └── test_stage1.py
1.2 数据库表结构（SQLite）
sql
-- db/migrations/001_create_parse_tasks.sql
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS parse_tasks (
    doc_hash TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL,               -- pending, processing, success, failed
    rag_status TEXT,                    -- null, pending, success, failed
    output_path TEXT NOT NULL,
    parser_version TEXT,
    trace_id TEXT,
    heartbeat TIMESTAMP,
    retry_count INTEGER DEFAULT 0,
    error TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(doc_hash, version)
);
CREATE INDEX idx_status_heartbeat ON parse_tasks(status, heartbeat);
1.3 核心代码骨架
db/task_repository.py
python
import aiosqlite
from datetime import datetime
from typing import Optional

class TaskRepository:
    def __init__(self, db_path: str = "./data/parse_tasks.db"):
        self.db_path = db_path

    async def create_task(self, doc_hash: str, content_hash: str, version: int,
                          output_path: str, trace_id: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO parse_tasks (doc_hash, content_hash, version, status, output_path, trace_id, heartbeat) "
                "VALUES (?, ?, ?, 'pending', ?, ?, ?)",
                (doc_hash, content_hash, version, output_path, trace_id, datetime.utcnow())
            )
            await db.commit()

    async def update_status(self, doc_hash: str, version: int, status: str, error: Optional[str] = None):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE parse_tasks SET status = ?, error = ?, updated_at = ? WHERE doc_hash = ? AND version = ?",
                (status, error, datetime.utcnow(), doc_hash, version)
            )
            await db.commit()

    async def update_rag_status(self, doc_hash: str, version: int, rag_status: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE parse_tasks SET rag_status = ?, updated_at = ? WHERE doc_hash = ? AND version = ?",
                (rag_status, datetime.utcnow(), doc_hash, version)
            )
            await db.commit()

    async def get_stale_processing_tasks(self, timeout_minutes: int = 30):
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT doc_hash, version FROM parse_tasks WHERE status = 'processing' "
                "AND datetime(heartbeat) < datetime('now', ?)",
                (f'-{timeout_minutes} minutes',)
            ) as cursor:
                return await cursor.fetchall()
pipeline/orchestrator.py 修改点
python
# 在 process_file 开始时
async def process_file(self, file_path: str):
    doc_hash = hashlib.sha256(open(file_path,'rb').read()).hexdigest()
    trace_id = str(uuid.uuid4())
    # 获取下一个版本号
    version = await task_repo.get_next_version(doc_hash)  # SELECT MAX(version)+1
    output_dir = f"/parsed_lib/{doc_hash}/{datetime.utcnow().isoformat()}/"
    await task_repo.create_task(doc_hash, "", version, output_dir, trace_id)

    try:
        # 原有解析逻辑...
        # 生成 blocks.jsonl, meta.json 等
        content_hash = compute_hash_of_output()
        await task_repo.update_content_hash(doc_hash, version, content_hash)
        await task_repo.update_status(doc_hash, version, "processing")
        # 强制 fsync 目录（可用 os.fsync）
        create_ready_file(output_dir)   # 原子创建 .ready
        # 通知 RAG
        rag_client = RAGClient()
        await rag_client.notify(doc_hash, version, output_dir, trace_id)
    except Exception as e:
        await task_repo.update_status(doc_hash, version, "failed", str(e))
        raise
rag/client.py
python
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

class RAGClient:
    def __init__(self, base_url: str = None, token: str = None):
        self.base_url = base_url or config.RAG_BASE_URL
        self.token = token or config.PARSER_TOKEN

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def notify(self, doc_hash: str, version: int, output_path: str, trace_id: str):
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/v1/ingestion/notify",
                headers={"X-Parser-Token": self.token},
                json={
                    "trace_id": trace_id,
                    "doc_hash": doc_hash,
                    "version": version,
                    "output_path": output_path,
                    "callback_url": f"{config.PARSER_CALLBACK_BASE}/v1/callback"
                }
            )
            response.raise_for_status()
            return response.json()
rag/callback_server.py (占位)
python
from fastapi import FastAPI, Header, HTTPException
import hmac

app = FastAPI()

@app.post("/v1/callback")
async def callback(payload: dict, x_parser_token: str = Header(None)):
    if not hmac.compare_digest(x_parser_token, config.PARSER_TOKEN):
        raise HTTPException(403, "Invalid token")
    # 本阶段只记录日志，不更新 DB（留待阶段4实现）
    print(f"Received callback: {payload}")
    return {"status": "ok"}
1.4 状态流转
text
[开始] → DB: status=pending
          ↓
    解析中 → 生成文件 + 更新 DB: status=processing + .ready
          ↓
    发送 notify → RAG 返回 202
          ↓
    (等待回调，本阶段回调仅记录)
1.5 可观测性
日志：每次状态变更输出 JSON 日志

json
{"timestamp":"...", "level":"INFO", "trace_id":"...", "event":"task_status_change", "doc_hash":"...", "version":1, "old_status":"pending", "new_status":"processing"}
指标：通过 prometheus_client 暴露

python
tasks_total = Counter('parser_tasks_total', 'Total parse tasks', ['status'])
notify_errors = Counter('parser_notify_errors_total', 'RAG notify errors')
审计：数据库 parse_tasks 表本身就是审计日志，可查询历史。

1.6 工具调用接口与权限边界
解析系统内部：无对外接口。

RAG 服务：POST /v1/ingestion/notify 需要 X-Parser-Token 验证。

回调接口 POST /v1/callback 同样需要 token 验证（阶段1仅占位）。

1.7 环境变量配置
bash
# .env
PARSER_TOKEN=shared-secret-change-me
RAG_BASE_URL=http://rag-service:8000
PARSER_CALLBACK_BASE=http://parser:8000
SHARED_STORAGE_PATH=/parsed_lib
DB_PATH=./data/parse_tasks.db
MAX_VERSIONS_TO_KEEP=3
HEARTBEAT_INTERVAL_SECONDS=300
ZOMBIE_TIMEOUT_MINUTES=30
1.8 验收测试
python
# tests/test_stage1.py
async def test_full_parse_flow():
    # 模拟上传一个 PDF
    await orchestrator.process_file("sample.pdf")
    # 检查 DB 中存在记录，状态至少为 processing
    task = await repo.get_task(doc_hash, version)
    assert task.status in ("processing", "success")
    # 检查 .ready 文件存在
    assert os.path.exists(f"/parsed_lib/{doc_hash}/{version_dir}/.ready")
    # 检查 RAG 通知被发送（mock RAG 服务可验证）
阶段 2：确定性约束求解核心（纯函数引擎）
2.1 模块与文件结构（RAG 服务侧）
text
rag_service/
├── ingestion/
│   ├── numeric_hint_extractor.py   # 从 JSONL 提取 hint
│   └── pipeline.py                 # 入库时存储 hint 到 Qdrant
├── evidence_builder/
│   └── builder.py                  # 从 chunks 构建候选约束
├── constraint_engine/
│   ├── normalizer.py               # 单位转换、同义词映射
│   ├── solver.py                   # 纯函数求解器
│   └── models.py                   # Constraint, NumericHint 等模型
├── api/routes/
│   └── constraints.py              # /v1/constraints 端点
├── session/
│   └── context_manager.py          # 上下文优先级合并
└── tests/
    ├── golden_set.json             # 黄金集用例
    └── test_stage2.py
2.2 核心数据结构（已在之前定义，此处强调）
python
# constraint_engine/models.py
class NumericHint(BaseModel):
    parameter: str = ""   # 可为空，运行时再解析
    value: float
    unit: str
    source_text: str
    source_span: Tuple[int, int]
    block_id: str
    page_num: Optional[int]
    scope_path: Optional[List[str]]   # 阶段3才使用

class Constraint(BaseModel):
    parameter: str
    operator: str   # <=, >=, ==, range
    value: Union[float, Tuple[float, float]]
    unit: str
    priority: int   # 100/80/60/40
    confidence: float
    source_hint_ids: List[str]
2.3 关键代码实现
ingestion/numeric_hint_extractor.py
python
import re
from .models import NumericHint

def extract_hints_from_text(text: str, block_id: str, page_num: int) -> List[NumericHint]:
    hints = []
    # 匹配数值+单位，例如 80°C, 0.5MPa
    pattern = r"(\d+(?:\.\d+)?)\s*([a-zA-Z°C%]+)"
    for match in re.finditer(pattern, text):
        value = float(match.group(1))
        unit = match.group(2)
        hints.append(NumericHint(
            parameter="",
            value=value,
            unit=unit,
            source_text=text[match.start():match.end()],
            source_span=(match.start(), match.end()),
            block_id=block_id,
            page_num=page_num,
        ))
    return hints
constraint_engine/normalizer.py
python
UNIT_CONVERSIONS = {
    "°C": ("C", lambda x: x),
    "K": ("C", lambda x: x - 273.15),
    "MPa": ("Pa", lambda x: x * 1e6),
    # ...
}

PARAMETER_SYNONYMS = {
    "温度": "temperature",
    "temp": "temperature",
    "压力": "pressure",
}

def normalize_unit(value: float, unit: str) -> Tuple[float, str]:
    if unit in UNIT_CONVERSIONS:
        target, func = UNIT_CONVERSIONS[unit]
        return func(value), target
    return value, unit

def normalize_parameter(raw: str) -> str:
    return PARAMETER_SYNONYMS.get(raw, raw)
constraint_engine/solver.py
python
def solve_group(constraints: List[Constraint]) -> dict:
    lower, upper = float("-inf"), float("inf")
    trace = []
    # 按优先级降序，同优先级按 confidence 降序
    sorted_c = sorted(constraints, key=lambda c: (c.priority, c.confidence), reverse=True)
    for c in sorted_c:
        prev_l, prev_u = lower, upper
        if c.operator == "<=":
            upper = min(upper, c.value)
        elif c.operator == ">=":
            lower = max(lower, c.value)
        elif c.operator == "==":
            lower = max(lower, c.value)
            upper = min(upper, c.value)
        elif c.operator == "range":
            lower = max(lower, c.value[0])
            upper = min(upper, c.value[1])
        if lower > upper:
            trace.append({"status": "conflict", "constraint": c.dict(), "rolled_back": True})
            lower, upper = prev_l, prev_u
        else:
            trace.append({"status": "applied", "new_range": [lower, upper]})
    return {
        "range": [None if lower == float("-inf") else lower, None if upper == float("inf") else upper],
        "conflict": lower > upper,
        "trace": trace
    }
api/routes/constraints.py
python
from fastapi import APIRouter, Body, HTTPException
from constraint_engine import normalize_parameter, normalize_unit, solve_group
from evidence_builder import build_constraints

router = APIRouter()

@router.post("/v1/constraints")
async def get_constraints(
    query: str = Body(...),
    context: dict = Body({}),
    strict: bool = Body(False),
    top_k: int = Body(40),
    session_id: Optional[str] = Body(None)
):
    # 1. 检索 chunks（调用现有检索核心）
    chunks = await retrieval.search(query, top_k)
    # 2. 收集所有 hints
    all_hints = []
    for chunk in chunks:
        all_hints.extend(chunk.get("numeric_hints", []))
    # 3. 构建候选约束（Evidence Builder）
    constraints = build_constraints(all_hints, context)
    # 4. 检查是否缺失上下文（strict 模式）
    if strict:
        required = detect_missing_context(constraints, context)
        if required:
            raise HTTPException(400, f"Missing required context: {required}")
    # 5. 分组求解
    grouped = {}
    for c in constraints:
        key = normalize_parameter(c.parameter)
        grouped.setdefault(key, []).append(c)
    results = {k: solve_group(v) for k, v in grouped.items()}
    # 6. 应用上下文优先级（构建 applied_context）
    applied_ctx = build_applied_context(context, chunks)
    return {
        "parameters": results,
        "applied_context": applied_ctx,
        "strict": strict,
        "trace_id": request.state.trace_id
    }
2.4 状态流转（无状态，每次请求独立）
无新增状态。

2.5 可观测性
日志：每次请求记录 trace_id, query, strict, applied_context, num_hints, solver_duration_ms。

指标：

python
constraint_requests = Counter('constraint_requests_total', 'Total constraint requests', ['strict_mode'])
solver_duration = Histogram('solver_duration_seconds', 'Solver execution time')
gate_failures = Counter('gate_failures_total', 'Gate failures', ['gate'])
2.6 工具调用接口与权限边界
/v1/constraints：不需要认证（或可选 API Key），供 Agent 调用。

内部调用检索核心：无额外权限。

2.7 黄金集测试（三层门禁）
python
# tests/golden_set.json
[
    {
        "name": "simple_temperature",
        "query": "温度不得超过80°C",
        "context": {},
        "strict": false,
        "expected": {
            "temperature": {"range": [null, 80], "unit": "C"}
        },
        "gates": {
            "recall": {"min_chunks": 1},
            "extraction": {"must_have_hint": {"value": 80, "unit": "C"}},
            "solve": {"range_upper": 80}
        }
    }
]
测试脚本：

python
def test_golden_set():
    for case in golden_set:
        # Gate1: 检索
        chunks = await retrieval.search(case["query"])
        assert len(chunks) >= case["gates"]["recall"]["min_chunks"]
        # Gate2: hint 提取
        hints = extract_hints_from_chunks(chunks)
        assert any(h.value == case["gates"]["extraction"]["must_have_hint"]["value"] for h in hints)
        # Gate3: 求解
        result = await get_constraints(case["query"], case["context"], case["strict"])
        assert result["parameters"]["temperature"]["range"][1] == case["gates"]["solve"]["range_upper"]
阶段 3：作用域感知与多分支分叉
3.1 模块修改
ingestion/numeric_hint_extractor.py：增加 scope_path 参数，从传入的 heading_stack 构建。

retrieval/scorer.py：新增 calculate_scope_match 和 rerank_with_scope。

api/routes/constraints.py：增加分叉检测逻辑，返回 branches。

3.2 作用域提取
python
# ingestion/numeric_hint_extractor.py
def extract_hints_from_block(block: dict, heading_stack: List[str]) -> List[NumericHint]:
    hints = []
    text = block["content"].get("md_preview", "")
    for match in re.finditer(r"(\d+(?:\.\d+)?)\s*([a-zA-Z°C%]+)", text):
        value = float(match.group(1))
        unit = match.group(2)
        hints.append(NumericHint(
            parameter="",
            value=value,
            unit=unit,
            source_text=match.group(),
            source_span=(match.start(), match.end()),
            block_id=block["block_id"],
            page_num=block["metadata"].get("page_number"),
            scope_path=heading_stack.copy()   # 存储当前标题栈
        ))
    return hints
3.3 检索重排序
python
# retrieval/scorer.py
def calculate_scope_match(query_terms: List[str], hint: NumericHint) -> float:
    if not query_terms or not hint.scope_path:
        return 0.0
    scope_text = " ".join(hint.scope_path).lower()
    matched = sum(1 for term in query_terms if term.lower() in scope_text)
    return matched / len(query_terms)

def rerank_with_scope(chunks: List[dict], query_scope_terms: List[str]) -> List[dict]:
    for chunk in chunks:
        semantic = chunk.get("score", 0.0)
        overlap = chunk.get("entity_overlap", 0.0)
        max_scope = 0.0
        for hint in chunk.get("numeric_hints", []):
            max_scope = max(max_scope, calculate_scope_match(query_scope_terms, hint))
        chunk["final_score"] = semantic + 0.2 * overlap + 0.15 * max_scope
    return sorted(chunks, key=lambda x: x["final_score"], reverse=True)
3.4 多分支输出
python
# api/routes/constraints.py
def group_by_scope(constraints: List[Constraint]) -> dict:
    groups = {}
    for c in constraints:
        key = tuple(c.scope_path) if c.scope_path else ("_default",)
        groups.setdefault(key, []).append(c)
    return groups

@router.post("/v1/constraints")
async def get_constraints(...):
    # ... 前面构建 constraints
    groups = group_by_scope(constraints)
    if not strict and len(groups) > 1:
        branches = []
        for scope, cons_list in groups.items():
            solved = {k: solve_group(v) for k, v in group_by_parameter(cons_list).items()}
            branches.append({
                "scope": " / ".join(scope),
                "parameters": solved,
                "confidence": sum(c.confidence for c in cons_list) / len(cons_list)
            })
        # 选择与 query_scope_terms 最匹配的分支作为默认
        default = select_default_branch(branches, query_scope_terms)
        return {"mode": "multi_branch", "branches": branches, "default_branch": default}
    else:
        # 单分支正常返回
        ...
3.5 验收测试
python
def test_scope_aware():
    # 上传包含多个章节的规范文档
    # 查询“高温环境温度限制”
    result = await get_constraints("高温环境温度限制", {}, strict=False)
    assert result["mode"] == "multi_branch"
    assert any("高温工况" in b["scope"] for b in result["branches"])
    # 默认分支应该是高温工况（如果 query 中有“高温”）
    assert "高温" in result["default_branch"]["scope"]
阶段 4：完整三系统集成与严格模式闭环
4.1 补充模块
rag/callback.py：实现真正更新 DB 的 rag_status。

rag/status_poller.py：解析系统启动时轮询 RAG 状态。

core/lock.py：Redis 分布式锁。

api/routes/ingestion.py：实现 GET /v1/ingestion/status/{doc_hash}。

4.2 回调完整实现
python
# rag/callback.py (RAG 服务侧)
@app.post("/v1/callback")
async def callback(payload: dict, x_parser_token: str = Header(None)):
    if not hmac.compare_digest(x_parser_token, config.PARSER_TOKEN):
        raise HTTPException(403)
    doc_hash = payload["doc_hash"]
    version = payload["version"]
    rag_status = payload["rag_status"]
    # 更新数据库（RAG 侧不需要数据库，而是通知解析系统？不对，回调是 RAG 调用解析系统）
    # 所以这里 RAG 应该调用解析系统的 callback_url
    async with httpx.AsyncClient() as client:
        await client.post(payload["callback_url"], json=payload, headers={"X-Parser-Token": config.PARSER_TOKEN})
    return {"status": "ok"}
实际上，回调逻辑应该是：RAG 入库成功后，RAG 服务主动调用解析系统提供的 callback_url。因此：

python
# RAG 服务在入库完成后
async def after_ingestion(doc_hash, version):
    await httpx.post(
        callback_url,
        json={"doc_hash": doc_hash, "version": version, "rag_status": "success", "trace_id": trace_id},
        headers={"X-Parser-Token": config.PARSER_TOKEN}
    )
4.3 解析系统状态轮询
python
# parser_system/rag/status_poller.py
async def poll_rag_status(doc_hash: str, version: int):
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{config.RAG_BASE_URL}/v1/ingestion/status/{doc_hash}?version={version}")
        data = resp.json()
        if data["rag_status"] == "success":
            await task_repo.update_rag_status(doc_hash, version, "success")
        else:
            # 重试
            await asyncio.sleep(10)
            await poll_rag_status(doc_hash, version)
4.4 分布式锁
python
# core/lock.py
import redis.asyncio as redis

class RedisLock:
    def __init__(self, redis_client, lock_key, token, timeout=600):
        self.redis = redis_client
        self.key = lock_key
        self.token = token
        self.timeout = timeout

    async def acquire(self):
        return await self.redis.set(self.key, self.token, nx=True, ex=self.timeout)

    async def release(self):
        # 使用 Lua 脚本保证原子性
        script = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("del", KEYS[1])
        else
            return 0
        end
        """
        await self.redis.eval(script, 1, self.key, self.token)
4.5 验收测试
python
async def test_full_cycle():
    # 解析系统处理文件
    await orchestrator.process_file("sample.pdf")
    # 模拟 RAG 入库完成并回调
    await callback_to_parser(doc_hash, version, "success")
    # 检查解析系统 DB 中 rag_status = success
    task = await repo.get_task(doc_hash, version)
    assert task.rag_status == "success"
阶段 5：可观测性与黄金集自动化
5.1 模块
observability/metrics.py：Prometheus 指标暴露。

observability/audit_logger.py：JSON 审计日志。

scripts/ci_gate.sh：CI 门禁脚本。

grafana/dashboards/ekrs.json：预置仪表盘。

5.2 指标暴露
python
# observability/metrics.py
from prometheus_client import Counter, Histogram, Gauge, generate_latest

constraint_requests = Counter('constraint_requests_total', 'Total requests', ['strict_mode'])
solver_duration = Histogram('solver_duration_seconds', 'Solver latency')
gate_failures = Counter('gate_failures_total', 'Gate failures', ['gate'])
active_sessions = Gauge('active_sessions', 'Active sessions')

async def metrics_endpoint(request):
    return Response(content=generate_latest(), media_type="text/plain")
5.3 审计日志
python
# observability/audit_logger.py
import logging
audit_logger = logging.getLogger("audit")
audit_logger.setLevel(logging.INFO)
handler = logging.FileHandler("/var/log/ekrs/audit.log")
handler.setFormatter(logging.Formatter('%(message)s'))
audit_logger.addHandler(handler)

def log_constraint_solving(trace_id, query, context, result, duration_ms):
    audit_logger.info(json.dumps({
        "trace_id": trace_id,
        "timestamp": datetime.utcnow().isoformat(),
        "query": query,
        "context": context,
        "result": result,
        "duration_ms": duration_ms
    }))
5.4 CI 门禁
bash
#!/bin/bash
# scripts/ci_gate.sh
set -e
pytest tests/golden_set.py --tb=short
if [ $? -ne 0 ]; then
    echo "Golden set failed, deployment blocked."
    exit 1
fi
echo "All gates passed."
5.5 验收测试
访问 /metrics 能看到指标。

运行 ci_gate.sh 在失败时退出码非 0。

审计日志文件持续写入。

总结
以上每个阶段均包含：

明确的模块文件列表

核心代码骨架（可直接复制使用）

API 规范与认证方式

状态流转描述

可观测性具体实现

验收测试用例

每个阶段可以独立运行，并满足“执行框架、约束条件、状态流转、可观测性”的要求。请根据实际项目进度选择阶段实施。

本回答由 AI 生成，内容仅供参考，请仔细甄别。
个阶段交付物清单和个阶段实现功能表呢
我们被要求提供“各阶段交付物清单”和“各阶段实现功能表”。需要将前面制定的分阶段计划（阶段1-5）整理成清晰的表格。用户强调“内容不齐全”，之前回复中缺少了这两个表格。因此需要补充这两个表格。同时确保内容具体、可执行。

我将输出两个表格：

各阶段交付物清单：每个阶段产出的文档、代码模块、配置文件、测试等。

各阶段实现功能表：每个阶段实现的具体功能点（对应铁律和需求）。

采用Markdown格式。

各阶段交付物清单
阶段	交付物类型	具体内容
阶段1	代码	db/ 模块（SQLite 初始化、TaskRepository）、pipeline/orchestrator.py 集成任务生命周期、rag/client.py（通知客户端）、rag/callback_server.py（回调占位）、services/heartbeat.py（僵尸监控）、services/version_cleanup.py（版本清理）、config.py 扩展
配置	.env 模板（含 PARSER_TOKEN, RAG_BASE_URL, DB_PATH, MAX_VERSIONS_TO_KEEP 等）、docker-compose.yml（解析系统 + SQLite）
测试	tests/test_stage1.py（任务创建、状态转换、僵尸检测、版本清理）
文档	docs/stage1.md（架构图、状态流转图、接口规范）
阶段2	代码	ingestion/numeric_hint_extractor.py、ingestion/pipeline.py（存储 hint 到 Qdrant）、evidence_builder/builder.py、constraint_engine/{normalizer,solver,models}.py、api/routes/constraints.py、session/context_manager.py
配置	config.py 新增 STRICT_MODE_DEFAULT, ENABLE_KG（预留）
测试	tests/golden_set.json（至少10个黄金用例）、tests/test_stage2.py（三层门禁单元测试）
文档	docs/stage2.md（约束求解 API 规范、黄金集使用说明）
阶段3	代码	ingestion/numeric_hint_extractor.py 增加 scope_path 提取、retrieval/scorer.py（作用域匹配重排序）、api/routes/constraints.py 增加分叉检测与多分支输出逻辑
配置	新增 SCOPE_MATCH_WEIGHT, FORKING_ENABLED 环境变量
测试	tests/golden_set.json 扩展作用域用例（多章节文档）、tests/test_stage3.py（作用域匹配、分支输出验证）
文档	docs/stage3.md（作用域机制设计、多分支交互说明）
阶段4	代码	rag/callback.py 完善（RAG 入库后回调解析系统）、rag/status_poller.py（解析系统启动时轮询）、api/routes/ingestion.py（状态查询接口）、core/lock.py（Redis 分布式锁）
配置	REDIS_URL, CALLBACK_RETRY_COUNT 环境变量
测试	tests/test_stage4.py（端到端回调、幂等性、并发锁、孤儿任务恢复）
文档	docs/stage4.md（三系统集成架构图、故障恢复流程）
阶段5	代码	observability/metrics.py（Prometheus 指标）、observability/audit_logger.py（JSON 审计日志）、scripts/ci_gate.sh（CI 门禁）、grafana/dashboards/ekrs.json（预置仪表盘）
配置	Prometheus 抓取配置、Grafana 数据源配置
测试	tests/test_stage5.py（指标端点、审计日志格式、CI 脚本退出码）
文档	docs/stage5.md（监控告警规则、日志分析指南、黄金集扩展规范）
各阶段实现功能表
阶段	功能模块	具体功能	对应铁律	验收标准
阶段1	数据库与任务状态	创建 parse_tasks 表，支持异步 CRUD	R1（证据化 Hint 前置）	可插入、更新、查询任务，唯一索引防冲突
原子输出信号	文件写入 → fsync → DB 更新 → .ready 原子顺序	R3（原子信号）	模拟崩溃后，不会出现无 .ready 的目录
RAG 通知	发送 POST /v1/ingestion/notify 带重试	R4（显式优先级）	通知失败自动重试3次，RAG 返回202
僵尸任务监控	后台线程扫描超时 processing 任务标记 failed	R5（瘦身 KG 无关）	模拟卡死任务，30分钟后被标记 failed
版本清理	保留最近 MAX_VERSIONS_TO_KEEP 个版本，删除其余	R6（严格模式无关）	创建4个版本，仅保留最新3个
阶段2	numeric_hint 提取	从 JSONL 提取数值+单位+位置，存储到 Qdrant payload	R1（证据化 Hint）	每个 hint 包含 source_span, block_id
Evidence Builder	从 chunks 和 hints 构建候选 Constraint 对象	R2（纯函数求解）	解析“80°C”生成 {parameter:"", value:80, unit:"C"}
单位与参数归一化	转换 MPa→Pa, °C→C, 温度→temperature	R2	求解时单位统一为 SI 或基准单位
纯函数求解器	交集求解、优先级排序、冲突检测、trace 记录	R2, R3	输入相同约束，输出确定且可重现
上下文优先级	合并 User > Explicit > Inferred > Default	R4	用户提供 material 覆盖文档默认值
严格模式	strict=true 时缺失上下文直接报错	R6	缺 material 时返回 400 错误
三层门禁测试	黄金集验证召回、hint 提取、求解三个阶段	R3	每次 PR 自动运行，失败则阻断
阶段3	作用域提取	从文档标题树生成 scope_path 存储于 hint	R7（作用域隔离）	每个 hint 携带章节路径
检索作用域匹配	计算 query 关键词与 scope_path 的匹配度，加权重排序	R7	“高温”查询优先返回高温章节的 chunk
多分支输出	检测多个不同作用域的高分约束，返回 branches 数组	R7	存在一般/高温两个分支时，返回分叉提示
默认分支选择	根据 query 关键词或用户上下文自动选择最匹配分支	R7	用户指定“高温”则默认选中高温分支
阶段4	RAG 回调实现	RAG 入库完成后调用解析系统 /v1/callback 更新 rag_status	R4	回调后解析系统 DB 中 rag_status=success
解析系统启动轮询	启动时查询所有 processing 任务，向 RAG 询问状态	R4	重启后丢失的回调被修复
状态查询接口	GET /v1/ingestion/status/{doc_hash}?version= 返回详情	R4	可供解析系统或 Agent 轮询
分布式锁	同一 doc_hash 并发入库时使用 Redis 锁防止重复	R3	两个相同通知只有一个执行入库
孤儿文件清理	定期扫描无对应 DB 记录的目录并删除	R3	残留 .ready 文件被清理
阶段5	Prometheus 指标	暴露请求数、耗时、门禁失败数、队列长度等	可观测性	/metrics 可被 Prometheus 抓取
审计日志	JSON Lines 格式记录每次求解的输入、输出、trace_id	可观测性	日志文件可导入 ELK 分析
CI 黄金集门禁	提交前自动运行黄金集，失败则阻止合并	R3	CI 脚本返回非0
Grafana 仪表盘	预置面板展示吞吐量、错误率、P99 延迟	可观测性	导入 JSON 后即可查看核心指标
告警规则	定义黄金集通过率 <100% 或求解器错误率 >1% 时告警	可观测性	Prometheus AlertManager 触发
这两个表格可直接作为项目里程碑的验收依据，确保每个阶段产出明确、功能可测。
