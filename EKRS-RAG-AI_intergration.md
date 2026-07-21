三系统集成规范（解析系统 ↔ RAG ↔ Agent）
1. 解析系统输出规范（修订 V2）
解析系统必须维护一个轻量型数据库（SQLite 或 PostgreSQL）作为版本与任务状态的权威记录，同时 RAG 服务维护可查询数据的真相。两者通过回调与状态同步，形成双真相模型。

1.1 数据库表结构（推荐）
sql
CREATE TABLE parse_tasks (
    doc_hash TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL,                -- pending, processing, success, failed
    rag_status TEXT,                     -- pending, success, failed (由RAG更新)
    output_path TEXT NOT NULL,
    generated_at TIMESTAMP NOT NULL,
    parser_version TEXT,
    trace_id TEXT,                       -- 用于关联日志
    heartbeat TIMESTAMP,                 -- 最后更新时间
    retry_count INTEGER DEFAULT 0,
    error TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 唯一约束防止并发版本冲突
CREATE UNIQUE INDEX idx_doc_version ON parse_tasks(doc_hash, version);
1.2 状态流转与版本控制
status：解析任务自身生命周期

pending → processing → success（解析完成）

失败时进入 failed

rag_status：由 RAG 服务回调更新，表示数据在 Qdrant 中的可用性

pending → success / failed

版本递增：

每次解析同一源文件，version 必须递增。

使用数据库唯一索引 (doc_hash, version) 防止并发冲突；若插入时冲突，由应用层重试（简单可靠）。

僵尸任务处理：

后台定时任务（每 10 分钟）扫描 status = processing 且 heartbeat < NOW() - 30分钟 的任务，将其标记为 failed 并记录错误。

解析进程应定期更新 heartbeat（例如每 5 分钟）。

1.3 原子操作与文件系统配合
严格顺序（必须遵循）：

生成文件：将 data.jsonl 和 meta.json 写入时间戳子目录（如 /parsed_lib/{doc_hash}/{timestamp}/）。

更新数据库：插入/更新 parse_tasks 记录，status = processing，填写 content_hash, version, output_path, trace_id 等。

确保落盘：调用 fsync 或确保文件已关闭（Python open + flush + os.fsync）。

创建 .ready：在目录中创建 .ready 空文件（原子操作）。**备注（2026-07-21 修正）**：`.ready` 文件由 parser 原子创建，作为 parser 侧的发布完成信号；RAG 服务**不读取** `.ready`，也**不扫描** `SHARED_STORAGE_PATH`。parser 必须在 JSONL 完整落盘、`fsync()` 完成后才发送 `POST /v1/ingestion/notify`；RAG 完全依赖 notify HTTP 触发。

RAG 入库通知：

发送 POST /v1/ingestion/notify，携带 doc_hash, version, output_path, trace_id。

RAG 收到通知后，开始异步入库。

回调与幂等性：

RAG 入库成功后，调用解析系统提供的 callback_url，请求体包含：

json
{
  "doc_hash": "...",
  "version": 3,
  "rag_status": "success",
  "trace_id": "..."
}
解析系统更新 parse_tasks 时，使用 WHERE doc_hash = ? AND version = ?，确保只有当前版本被更新，避免旧版本覆盖新版本。若版本不匹配，记录警告但不报错。

RAG 端应实现指数退避重试（至少 3 次）。

兜底查询：

解析系统启动时，主动查询所有 rag_status = pending 且 status = success 的任务，询问 RAG 状态接口（GET /v1/ingestion/status/{doc_hash}），以应对回调丢失。

1.4 存储清理（职责划分）
解析系统：负责标记可清理的旧版本，但不直接删除文件。

定期（如每天）将 version < current_version - MAX_VERSIONS_TO_KEEP 且 rag_status = success 的记录标记为 status = archived。

RAG 服务：负责在版本切换后，异步删除 Qdrant 中的旧版本数据，完成后回调解析系统（或提供一个删除接口），由解析系统执行实际文件删除。

孤儿文件清理：定期扫描 /parsed_lib/ 目录，删除那些在数据库中无对应记录（或 status = archived 且文件存在时间超过保留期）的文件夹。

1.5 日志与追踪
每个解析任务必须生成唯一的 trace_id，在数据库、文件 meta.json 以及所有 HTTP 调用（通知、回调）中传递。

RAG 服务应将 trace_id 记录在日志中，以便端到端追踪。

2. RAG 入库流程（修订，含版本控制与无缝切换）
接口：POST /v1/ingestion/notify

处理步骤：

验证 X-Parser-Token，检查 doc_hash 是否已存在。
使用 Redis 分布式锁（带 token）锁定 doc_hash：
SET lock:{doc_hash} {token} NX PX 600000（10分钟超时）
查询 Qdrant 中是否存在相同 doc_hash 且 content_hash 的数据（通过 payload 过滤）。若存在，则跳过本次入库（幂等），释放锁，返回 200 OK。
否则，执行新版本入库：
将新数据写入 Qdrant，每个 chunk 的 payload 中包含：
doc_hash
version = 从通知中获取的 version
content_hash
写入完成后，删除 Qdrant 中同一 doc_hash 但 version 小于新版本的数据（通过 delete 操作过滤 doc_hash 和 version）。
更新 Redis 中的任务状态为 SUCCESS，并记录当前版本信息（仅用于缓存，不作为真相源）。
在 finally 块中释放锁（仅当 token 匹配时释放）。
注意事项：

查询时，RAG 服务使用 Redis 缓存的最新 content_hash 或直接从 Qdrant 检索（因为旧版本已被删除，无需额外过滤）。

若删除旧版本失败，不影响新版本查询；可异步重试删除。

3. 轮询降级扫描（修订）
~~扫描 /parsed_lib/*/{timestamp}/.ready，对每个 .ready 文件，读取 meta.json 中的 content_hash 和 version。~~

**备注（2026-07-21 修正）**：RAG 服务**没有** `.ready` 轮询扫描器；本节为早期设计遗留，与当前实现不符。RAG 完全依赖 notify HTTP 触发；meta.json 也非本期契约——本期仅按 `output_path/data.jsonl` 精确读取，并通过 `notify` body 的 `metadata.doc_metadata` 字段透传 doc 元数据。若 Qdrant 中不存在该 content_hash 的数据，则触发入库（与通知逻辑相同，使用锁防止重复）。

入库成功后，~~不要删除 .ready，而是重命名为 .processed，或保留 .ready 但记录已处理~~；`.processed` 命名约定同样未实施——RAG 通过 `doc_hash` + `version` 在 Qdrant 中执行幂等检查（`get_ingestion_status`），无需任何文件系统状态标记。

4. 解析系统 ↔ RAG 接口定义
4.1 解析系统 → RAG
POST /v1/ingestion/notify：解析系统完成写入后调用。

Headers: X-Parser-Token: <shared_secret>

Body:

json
{
  "trace_id": "uuid",
  "doc_hash": "sha256",
  "version": 3,
  "output_path": "/parsed_lib/abc123/2025-04-02T10-30-00Z/",
  "metadata": { "filename": "spec.pdf" },
  "callback_url": "https://parser/v1/callback"
}
Response: 202 Accepted {"status": "queued"}

GET /v1/ingestion/status/{doc_hash}：查询入库状态。

Response: {"status": "processing|success|failed", "chunks_indexed": 42, "version": 3, "error": null}

4.2 RAG → 解析系统回调
POST {callback_url}：RAG 入库完成后调用。

Body:

json
{
  "doc_hash": "...",
  "version": 3,
  "rag_status": "success",
  "trace_id": "..."
}
解析系统更新 parse_tasks 的 rag_status，使用 WHERE doc_hash = ? AND version = ? 确保幂等。

4.3 RAG → 客户端（同步查询）
POST /v1/constraints：Agent 或客户端发送查询，返回结构化约束。

Request:

json
{
  "session_id": "optional_uuid",
  "query": "混凝土养护温度不得超过80°C，压力不低于0.5MPa",
  "top_k": 40,
  "mode": "hybrid",
  "rerank": false,
  "filters": {},
  "kg_enhance": false,
  "context": { "material": "Q345" }
}
Response:

json
{
  "session_id": "...",
  "parameters": {
    "temperature": {
      "range": [null, 80],
      "unit": "C",
      "priority": 100,
      "confidence": 0.92,
      "sources": [{"doc_id": "abc", "block_id": "uuid"}],
      "conflicts": [],
      "trace": [...]
    }
  },
  "total_evidence": 12,
  "evidence_preview": [{"text": "...", "source": "doc_id"}]
}
4.4 会话管理
POST /v1/sessions → 创建会话，返回 session_id

GET /v1/sessions/{session_id} → 获取累积的约束

DELETE /v1/sessions/{session_id} → 销毁会话

支持内存存储（默认）和 Redis 存储（配置切换）

🧠 RAG 检索核心（轻量，无推理）
语义分块（从 JSONL）
维护标题栈，合并连续 text 块，遇到 table/kv 单独成块

最大 500 token（可配置）

每个分块 payload 包含：

text：原始文本（从 md_preview 或 raw）

heading_path：标题层级列表

doc_hash，block_ids

source_metadata（页号、文件名等）

numeric_hints：见下文规范

嵌入与检索
模型：bge-m3 (ONNX, CPU)，输出稠密（1024d）+ 稀疏向量

混合检索：dense + sparse，默认 top_k = 40（高召回）

可选重排：bge-reranker-base 仅对 top 20 做二次排序，但不减少候选数

禁止在检索核心中做任何约束提取或推理

🧩 Ingestion 阶段的 Numeric Hint（必须实现）
目的：以极轻量、无损的方式预提取数值锚点，加速 runtime 解析，但不做任何语义理解。

规范
json
{
  "parameter_hint": "string",   // 原始文本片段，不归一化
  "value": number,
  "unit": "string",
  "span": [start, end],         // 字符偏移量，相对于 chunk.text
  "source_text": "string"       // 可选，短上下文
}
约束：

禁止提取：operator（<=, >= 等）、条件（if, unless）、逻辑关系

禁止参数名归一化（保持原始形式）

必须用确定性规则（正则或简单解析器）提取

同一句子中多数值：例如“压力 0.8–1.0 MPa”应生成两个 hint（0.8 和 1.0）

示例：

输入："温度不得超过80°C"

输出：{"parameter_hint": "温度", "value": 80, "unit": "C", "span": [0, 10]}

🧩 Evidence Builder（必须实现）
位置：rag_service/evidence_builder/

职责：将检索到的 chunks 转换为候选约束列表。

流程：

去重：按 doc_hash + 内容哈希去重

利用 numeric hints 快速定位：对每个 hint，读取其 span 对应的原文片段，调用 ConstraintParser 进行局部解析（确定 operator、条件等）

参数归一化：使用同义词映射表（如 temperature|temp|温度 → temperature）

输出：List[Constraint]

⚙️ 约束引擎（完全确定性，无 LLM）
数据模型（constraint_engine/models.py）
python
from pydantic import BaseModel
from typing import List, Optional, Union, Tuple, Any
from enum import IntEnum

class Priority(IntEnum):
    NATIONAL = 100   # 国标
    INDUSTRY = 80    # 行标
    ENTERPRISE = 60  # 企标
    PROJECT = 40     # 项目/合同
    REFERENCE = 20   # 参考

class Condition(BaseModel):
    parameter: str
    operator: str    # ==, >, <, contains
    value: Any

class Constraint(BaseModel):
    parameter: str
    operator: str    # <=, >=, ==, range
    value: Union[float, Tuple[float, float]]
    unit: str
    category: str = "general"
    priority: Priority = Priority.PROJECT
    conditions: List[Condition] = []
    confidence: float = 1.0
    reference_event: Optional[str] = None
    is_working_day: bool = False
    source: dict = {}
    # 版本和来源（仅用于追溯，不用于离线修复）
    version: Optional[int] = None
    content_hash: Optional[str] = None
单位转换引擎（normalizer.py）
必须实现 UnitRegistry 类，支持以下类别及单位（可扩展）：

python
class UnitRegistry:
    CONVERSIONS = {
        "length": {
            "m": 1.0, "mm": 0.001, "cm": 0.01, "km": 1000.0,
            "in": 0.0254, "ft": 0.3048, "米": 1.0, "毫米": 0.001
        },
        "area": {
            "m2": 1.0, "mm2": 1e-6, "cm2": 1e-4, "km2": 1e6,
            "hectare": 10000.0, "acre": 4046.86, "平方米": 1.0, "公顷": 10000.0, "亩": 666.67
        },
        "time_duration": {
            "d": 1.0, "h": 1/24, "min": 1/1440, "s": 1/86400,
            "week": 7.0, "month": 30.0, "year": 365.0,
            "天": 1.0, "日": 1.0, "小时": 1/24, "个月": 30.0, "年": 365.0
        },
        "pressure": {"pa": 1.0, "mpa": 1e6, "kpa": 1e3, "bar": 1e5, "psi": 6894.76},
        "temperature": {"c": 1.0, "k": 1.0, "f": 1.0}
    }

    @classmethod
    def normalize(cls, value: float, unit: str) -> Tuple[float, str, str]:
        # 清洗单位，处理非线性温度转换
        ...
    
    @staticmethod
    def parse_time_deadline(text: str) -> dict:
        """解析如 '开工后30天内' -> {'reference_event': '开工', 'offset_days': 30, 'is_working_day': False}"""
        pattern = r"(.*)(?:后|起)\s*(\d+)\s*(天|日|个月|周|工作日)"
        ...
约束解析器（parser.py）
python
class ConstraintParser:
    def parse_block(self, block: dict, hint: Optional[dict] = None) -> List[Constraint]:
        if block["type"] == "table":
            return self._parse_table(block["content"]["structured"], block)
        elif block["type"] == "kv":
            return self._parse_kv(block["content"]["structured"], block)
        else:
            text = block["content"].get("md_preview", "")
            if hint:
                start, end = hint["span"]
                text = text[start:end]
            return self._parse_text(text, block, hint)

    def _parse_table(self, table_data: List[List], meta: dict) -> List[Constraint]:
        # 首行作为参数名，后续每行提取值，同行其他列作为条件
        ...
    
    def _parse_kv(self, kv_data: dict, meta: dict) -> List[Constraint]:
        ...
    
    def _parse_text(self, text: str, meta: dict, hint: dict) -> List[Constraint]:
        # 优先检测时间期限
        time_data = UnitRegistry.parse_time_deadline(text)
        if time_data:
            return [Constraint(parameter="deadline", operator="<=", value=time_data["offset_days"], ...)]
        # 正则匹配: 不得超过|不低于|范围是...，并调用 UnitRegistry.normalize
        ...
求解器（solver.py）
python
def solve_parameter_group(constraints: List[Constraint]) -> dict:
    # 1. 按优先级降序，同优先级按 confidence 降序
    sorted_cons = sorted(constraints, key=lambda x: (x.priority, x.confidence), reverse=True)
    lower, upper = float("-inf"), float("inf")
    trace = []
    sources = []
    for c in sorted_cons:
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
            lower, upper = prev_l, prev_u  # 回滚
        else:
            trace.append({"status": "applied", "new_range": [lower, upper], "source": c.source})
        sources.append(c.source)
    return {
        "range": [None if lower == float("-inf") else lower, None if upper == float("inf") else upper],
        "is_conflict": lower > upper,
        "trace": trace,
        "sources": sources
    }
依赖图（可选，V1 简化）
python
def build_dependency_graph(constraints: List[Constraint]) -> dict:
    graph = defaultdict(set)
    for c in constraints:
        if c.conditions:
            for cond in c.conditions:
                graph[cond.parameter].add(c.parameter)
    return graph
引擎编排（engine.py）
python
def run_engine(chunks: List[dict], context: dict) -> dict:
    builder = EvidenceBuilder()
    constraints = builder.build(chunks)
    filtered = filter_constraints_by_conditions(constraints, context)
    grouped = group_by_parameter(filtered)
    results = {param: solve_parameter_group(cons) for param, cons in grouped.items()}
    return {"parameters": results, "total_evidence": len(constraints)}
💾 会话管理（session_manager.py）
支持两种后端：

InMemoryStore：用于单机、开发

RedisStore：用于生产、多实例

接口：

python
class SessionStore(ABC):
    def get(self, session_id) -> Optional[dict]
    def set(self, session_id, data)
    def delete(self, session_id)
📦 消息队列与批量入库
RAG 服务必须支持通过消息队列异步处理入库任务，以应对高并发和批量提交。

队列类型：支持 memory（默认）、redis、rabbitmq，通过 QUEUE_TYPE 配置。

队列实现：抽象基类 BaseQueue，分别实现 MemoryQueue, RedisQueue, RabbitMQQueue。

Worker 管理：INGESTION_WORKERS 控制并发数量（默认 2）。

任务失败重试：失败任务进入死信队列（重试 3 次后），记录错误日志。

📊 可观测性与审计
日志
使用结构化日志（JSON 格式），每条日志包含：

timestamp

level

module

trace_id

doc_hash（如果适用）

message

duration_ms

指标（Prometheus）
必须暴露以下指标（/metrics 端点）：

指标名称	类型	说明
rag_ingestion_total	Counter	入库任务总数（按状态 label）
rag_ingestion_duration_seconds	Histogram	入库耗时
rag_retrieve_duration_seconds	Histogram	检索耗时
rag_constraint_solve_duration_seconds	Histogram	约束求解耗时
rag_queue_size	Gauge	队列长度
rag_worker_active	Gauge	活跃 Worker 数
rag_db_operation_errors	Counter	数据库操作错误数
分布式追踪（可选）
支持 OpenTelemetry，通过环境变量 ENABLE_TRACING=true 启用。

导出到 Jaeger 或 OTEL Collector。

审计
所有入库的约束必须记录：

doc_hash

block_id

原始文本

提取的 Constraint 对象（含 source）

处理时间

审计日志独立存储（可写至文件或数据库），保留至少 30 天。

支持通过 GET /v1/audit/{doc_hash} 接口查询（需管理员权限）。

