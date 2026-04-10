工程知识恢复系统 (EKRS) 开发规范 V2.3
版本历史
版本	日期	作者	变更内容
V2.0	2026-04-02	架构组	初始版本，基于七铁律+六补丁
V2.1	2026-04-02	架构组	增加分阶段实施细节、代码示例、验收标准
V2.2	2026-04-09	架构组	整合技术栈明细、接口定义、DERE核心算法实现，修正内部逻辑错误
V2.3	2026-04-09	架构组	新增开发调试 UI 设计、开发阶段日志规范
1. 背景与目标
1.1 业务痛点
工程规范、标准、图纸中的约束（温度、压力、材料等）分散在大量非结构化文档中。

传统 RAG 系统只能检索文本片段，无法提取结构化约束、无法处理单位换算、无法解决跨文档冲突。

工程师需要可追溯、可重现、高精度的约束答案，而不是 LLM 的“幻觉”。

1.2 系统目标
构建工程知识恢复系统 (Engineering Knowledge Recovery System, EKRS)，核心能力：

从文档（PDF/Word/DWG）中提取数值锚点（numeric_hint），保留证据链。

通过确定性求解器（纯函数）计算参数可行范围，支持优先级、冲突检测。

实现作用域感知，区分同一参数在不同章节/工况下的不同约束。

提供严格模式（无推断）和智能推断模式，满足不同工程场景。

全链路可审计、可重现（Replay 模式）。

1.3 系统边界
输入：解析系统输出的 JSONL（DocumentBlock IR）。

输出：结构化约束（参数、范围、单位、来源、冲突）。

不包含：LLM 生成自然语言回答、外部知识库自动更新、多模态识别。

2. 七铁律（Seven Iron Rules）
编号	铁律	描述	验证方式
R1	证据化 Hint	每个 numeric_hint 必须包含 source_span、block_id、context_window	检查入库 payload
R2	纯函数 Solver	Solver(hints, context) → result 无 I/O、无状态、无副作用	单元测试确定性
R3	三层门禁	召回 → hint 提取 → 求解，全链路审计，任一失败则阻断	黄金集测试
R4	显式优先级	上下文合并：User > Explicit_Doc > Inferred_Doc > Default	输出中显示来源
R5	轻量 KG	仅 Entity Overlap 评分，无多跳推理	无图数据库依赖
R6	严格模式	strict=true 时禁止任何推断，缺条件即报错	API 测试
R7	作用域隔离	每个 hint 携带 scope_path，检索时按需启用作用域匹配	多分支输出测试
3. 整体架构
text
┌─────────────────────────────────────────────────────────────────┐
│                        解析系统 (Parser)                         │
│  - 文档结构化 → JSONL                                           │
│  - 提取 numeric_hint（含 scope_path, context_window）            │
│  - 写入共享目录 + SQLite 任务状态                                │
└─────────────────────────────────────────────────────────────────┘
                              │
                              │ 通知 (HTTP)
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                         RAG 服务                                 │
│  - 检索相关 chunks（Qdrant）                                    │
│  - 作用域感知重排序                                              │
│  - Evidence Builder → 约束列表                                   │
│  - 纯函数求解器                                                  │
│  - 输出结构化结果 + 审计日志                                     │
└─────────────────────────────────────────────────────────────────┘
                              │
                              │ 回调 + 状态查询
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                     Agent / 客户端                               │
│  - 发送查询（含 context, strict 标志）                           │
│  - 展示多分支结果（如有）                                        │
│  - 支持 replay 模式                                              │
└─────────────────────────────────────────────────────────────────┘
4. 核心数据模型
4.1 解析系统数据库表 parse_tasks
sql
CREATE TABLE parse_tasks (
    doc_hash TEXT NOT NULL,
    content_hash TEXT NOT NULL,          -- 内容指纹
    version INTEGER NOT NULL,
    status TEXT NOT NULL,                -- pending, processing, success, failed
    rag_status TEXT,                     -- null, pending, success, failed
    output_path TEXT NOT NULL,
    parser_version TEXT,
    trace_id TEXT,
    heartbeat TIMESTAMP,
    retry_count INTEGER DEFAULT 0,
    error TEXT,
    is_active BOOLEAN DEFAULT TRUE,      -- 当前活跃版本
    rag_trace_id TEXT,                   -- 回调追踪
    rag_updated_at TIMESTAMP,            -- 回调时间
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(doc_hash, content_hash)       -- 唯一约束
);
CREATE INDEX idx_active ON parse_tasks(doc_hash, is_active);
4.2 RAG 服务数据模型（Pydantic）
python
class NumericHint(BaseModel):
    parameter: str = ""                 # 原始参数名（可空）
    value: float
    unit: str
    source_text: str
    source_span: Tuple[int, int]        # 字符级索引 [start, end)
    block_id: str
    page_num: Optional[int]
    scope_path: List[str] = []          # 文档层级路径
    context_window: str = ""            # 前后20字符

class Constraint(BaseModel):
    parameter: str
    operator: str                       # <=, >=, ==, range
    value: Union[float, Tuple[float, float]]
    unit: str
    priority: int                       # 100/80/60/40
    confidence: float
    source_hint_ids: List[str]          # 关联的 hint 标识
    scope_path: List[str] = []
5. API 接口规范
5.1 解析系统 → RAG 通知
http
POST /v1/ingestion/notify
Headers: X-Parser-Token: <shared_secret>
Body:
{
  "trace_id": "550e8400-e29b-41d4-a716-446655440000",
  "doc_hash": "sha256",
  "version": 3,
  "output_path": "/parsed_lib/abc123/2026-04-02T10-30-00Z/",
  "callback_url": "http://parser:8000/v1/callback"
}
Response: 202 Accepted { "status": "queued" }
5.2 RAG → 解析系统回调（幂等）
http
POST {callback_url}
Headers: X-Parser-Token: <shared_secret>
Body:
{
  "doc_hash": "sha256",
  "version": 3,
  "rag_status": "success",
  "trace_id": "uuid"
}
5.3 约束查询 API
http
POST /v1/constraints
Content-Type: application/json
Body:
{
  "query": "高温环境下温度限制",
  "context": { "material": "Q345" },
  "strict": false,
  "replay": false,
  "trace_id": "optional",
  "top_k": 40
}
响应（单分支）：

json
{
  "trace_id": "abc123",
  "replay": false,
  "mode": "single",
  "parameters": {
    "temperature": { "range": [null, 80], "unit": "C", "confidence": 0.95 }
  },
  "applied_context": { "material": { "value": "Q345", "source": "user" } },
  "strict": false
}
响应（多分支）：

json
{
  "trace_id": "abc123",
  "replay": false,
  "mode": "multi_branch",
  "branches": [
    { "scope": "一般工况", "parameters": { "temperature": { "range": [null, 80], "unit": "C" } }, "confidence": 0.92 },
    { "scope": "高温工况", "parameters": { "temperature": { "range": [null, 120], "unit": "C" } }, "confidence": 0.96 }
  ],
  "default_branch": "高温工况",
  "applied_context": { "material": { "value": "Q345", "source": "user" } },
  "strict": false
}
5.4 状态查询
http
GET /v1/ingestion/status/{doc_hash}?version=3
Response:
{
  "doc_hash": "...",
  "version": 3,
  "rag_status": "success",
  "chunks_indexed": 42,
  "last_updated": "2026-04-02T12:00:00Z"
}
6. 分阶段实施计划
阶段	目标	核心交付物	验收标准
阶段 1	基础底座（数据库、版本控制、原子信号）	task_repository.py、orchestrator.py、回调服务器、心跳监控、清理脚本	同一源文件两次解析产生两条记录；僵尸任务30分钟后标记 failed
阶段 2	确定性约束求解核心	numeric_hint_extractor.py、evidence_builder、纯函数求解器、context_manager、黄金集测试	相同输入多次求解结果一致；严格模式缺条件返回 400
阶段 3	作用域感知与多分支	作用域提取、检索重排序、分叉检测、多分支输出	查询“高温环境温度限制”返回两个分支且默认选中“高温工况”
阶段 4	三系统集成与闭环	回调幂等、状态轮询、分布式锁、补偿任务、孤儿清理	回调失败后补偿任务修复状态；并发通知只有一个执行入库
阶段 5	可观测性与黄金集自动化	Prometheus 指标、审计日志、CI 门禁、Replay 模式	/metrics 可被抓取；CI 脚本阻断部署；Replay 结果完全一致
7. 技术栈明细与接口细化
7.1 总体技术栈
组件	技术选型	版本/备注	用途
解析系统 (Parser)	Python 3.11+		任务编排、文件处理、数据库
aiosqlite	0.20.0	异步 SQLite 驱动
FastAPI	0.115.0	提供回调接口
httpx	0.27.0	异步 HTTP 客户端
tenacity	8.5.0	重试机制
RAG 服务	Python 3.11+		API、检索、求解
FastAPI	0.115.0	提供约束查询、通知接收接口
Qdrant	1.11.0 (Docker)	向量数据库，支持 dense + sparse
bge-m3	ONNX, CPU	嵌入模型（dense 1024d，sparse 词权重）
bge-reranker-base	可选	重排序（阶段2以后）
aiosqlite	0.20.0	自身状态存储
redis-py	5.0.1	分布式锁（阶段4）
可观测性	prometheus-client	0.19.0	指标暴露
python-json-logger	2.0.7	结构化日志
opentelemetry-api	1.23.0	可选追踪
测试	pytest	8.0.0	单元/集成测试
pytest-asyncio	0.23.0	异步测试
部署	Docker / Docker Compose	24.0+	容器化
Kubernetes	1.28+	可选生产编排
7.2 Qdrant 集合配置示例
python
from qdrant_client import QdrantClient, models

client = QdrantClient(host="localhost", grpc_port=6334)
client.create_collection(
    collection_name="rag_documents",
    vectors_config={
        "dense": models.VectorParams(size=1024, distance=models.Distance.COSINE),
    },
    sparse_vectors_config={
        "sparse": models.SparseVectorParams()
    }
)
7.3 黄金集格式（tests/golden_set.json）
json
[
  {
    "name": "simple_temperature",
    "query": "温度不得超过80°C",
    "context": {},
    "strict": false,
    "expected": {
      "temperature": { "range": [null, 80], "unit": "C" }
    },
    "gates": {
      "recall": { "min_chunks": 1 },
      "extraction": { "must_have_hint": { "value": 80, "unit": "C", "operator": "LE" } },
      "solve": { "range_upper": 80 }
    }
  }
]
7.4 环境变量最小集
bash
# 通用
PARSER_TOKEN=shared-secret-change-me
RAG_BASE_URL=http://rag-service:8000
SHARED_STORAGE_PATH=/parsed_lib
DB_PATH=./data/parse_tasks.db

# 版本控制
MAX_VERSIONS_TO_KEEP=3
HEARTBEAT_INTERVAL_SECONDS=300
ZOMBIE_TIMEOUT_MINUTES=30

# 回调
PARSER_CALLBACK_BASE=http://parser:8000

# Redis（阶段4）
REDIS_URL=redis://redis:6379
RECONCILE_INTERVAL_SECONDS=600

# 作用域
SCOPE_MATCH_WEIGHT=0.15
FORKING_ENABLED=true

# 严格模式默认
STRICT_MODE_DEFAULT=false
8. 确定性证据重建引擎 (DERE) 核心实现
本部分提供确定性求解、冲突归因、作用域过滤、单位归一化的生产级代码实现。

8.1 单位归一化与算子映射 (normalizer.py)
python
import portion as P

UNIT_MAP = {
    "MPa": 1_000_000,
    "Pa": 1,
    "bar": 100_000,
    "psi": 6894.76,
    "C": 1.0,    # 温度涉及偏移，特殊处理
}

OPERATOR_MAP = {
    "不超过": lambda v: P.closed(-P.inf, v),
    "小于": lambda v: P.open(-P.inf, v),
    "不低于": lambda v: P.closed(v, P.inf),
    "大于": lambda v: P.open(v, P.inf),
    "等于": lambda v: P.singleton(v),
}

def normalize_unit(value: float, unit: str) -> tuple[float, str]:
    """返回 (归一化值, 基础单位)"""
    if unit in UNIT_MAP:
        return value * UNIT_MAP[unit], "Pa" if unit in ["MPa", "Pa", "bar", "psi"] else unit
    return value, unit

def normalize_parameter(param: str) -> str:
    """参数名同义词映射，如 'temp' -> 'temperature'"""
    synonyms = {"temp": "temperature", "press": "pressure"}
    return synonyms.get(param.lower(), param.lower())
8.2 求解器核心逻辑 (solver.py)
python
import portion as P
from typing import List, Dict, Optional
from .models import NumericHint, Operator
from .normalizer import normalize_unit, normalize_parameter

class IntervalSolver:
    @staticmethod
    def to_interval(hint: NumericHint) -> P.Interval:
        value_norm, _ = normalize_unit(hint.value, hint.unit)
        if hint.operator == Operator.LE:
            return P.closed(-P.inf, value_norm)
        elif hint.operator == Operator.GE:
            return P.closed(value_norm, P.inf)
        elif hint.operator == Operator.EQ:
            return P.singleton(value_norm)
        else:
            raise ValueError(f"Unsupported operator: {hint.operator}")

    @classmethod
    def solve(cls, hints: List[NumericHint], active_scope: Optional[List[str]] = None) -> Dict[str, dict]:
        groups: Dict[str, List[NumericHint]] = {}
        for hint in hints:
            # 作用域过滤
            if active_scope is not None:
                if not hint.evidence.scope_path[:len(active_scope)] == active_scope:
                    continue
            param = normalize_parameter(hint.parameter)
            groups.setdefault(param, []).append(hint)   # 注意：方法名是 setdefault

        results = {}
        for param, hint_list in groups.items():
            total_interval = P.closed(-P.inf, P.inf)
            applied_hints = []
            conflict_info = None

            for h in hint_list:
                new_interval = cls.to_interval(h)
                temp_interval = total_interval & new_interval
                if temp_interval.empty:
                    conflict_info = {
                        "status": "CONFLICT",
                        "reason": f"Constraint '{h.operator} {h.value}{h.unit}' conflicts with prior constraints",
                        "offending_hint": h,
                        "prior_hints": applied_hints.copy()
                    }
                    break
                total_interval = temp_interval
                applied_hints.append(h)

            if conflict_info:
                results[param] = {
                    "status": "CONFLICT",
                    "conflict_reason": conflict_info["reason"],
                    "offending_evidence": conflict_info["offending_hint"].evidence,
                    "prior_evidence": [h.evidence for h in conflict_info["prior_hints"]]
                }
            else:
                if not hint_list:
                    continue
                _, unit = normalize_unit(hint_list[0].value, hint_list[0].unit)
                results[param] = {
                    "status": "OK",
                    "interval": total_interval,
                    "unit": unit,
                    "evidence": [h.evidence for h in applied_hints]
                }
        return results
8.3 审计日志扩展 (audit.py)
python
import json
from datetime import datetime
from typing import List
from .models import Evidence

class AuditLogger:
    def __init__(self, log_path: str):
        self.log_path = log_path

    def log_constraint(self, constraint_id: str, parameter: str, interval, unit: str,
                       evidence_list: List[Evidence], context: dict, steps: list):
        entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "constraint_id": constraint_id,
            "parameter": parameter,
            "interval": str(interval),
            "unit": unit,
            "evidence": [{"text": e.text, "doc": e.doc_hash, "span": e.span} for e in evidence_list],
            "applied_context": context,
            "steps": steps
        }
        with open(self.log_path, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def log_conflict(self, conflict_id: str, parameter: str, conflict_reason: str,
                     offending_evidence: Evidence, prior_evidence: List[Evidence], context: dict):
        entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "conflict_id": conflict_id,
            "parameter": parameter,
            "reason": conflict_reason,
            "offending_evidence": {
                "text": offending_evidence.text,
                "doc": offending_evidence.doc_hash,
                "span": offending_evidence.span,
                "scope": offending_evidence.scope_path
            },
            "prior_evidence": [
                {"text": e.text, "doc": e.doc_hash, "span": e.span, "scope": e.scope_path}
                for e in prior_evidence
            ],
            "applied_context": context
        }
        with open(self.log_path, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
8.4 流水线集成示例 (pipeline.py)
python
import hashlib
from typing import Optional, Dict, List
from .extractor import NumericHintExtractor
from .solver import IntervalSolver
from .audit import AuditLogger

class DEREPipeline:
    def __init__(self, audit_log_path: str = "audit.log"):
        self.extractor = NumericHintExtractor()
        self.solver = IntervalSolver()
        self.audit = AuditLogger(audit_log_path)

    def _stable_hash(self, *args) -> str:
        content = "|".join(str(arg) for arg in args)
        return hashlib.md5(content.encode()).hexdigest()[:12]

    def run(self, text: str, doc_hash: str, scope_path: List[str],
            page_num: Optional[int] = None,
            user_context: Optional[Dict] = None,
            active_scope: Optional[List[str]] = None) -> Dict:
        hints = self.extractor.extract(text, doc_hash, scope_path, page_num)
        results = self.solver.solve(hints, active_scope=active_scope)

        for param, data in results.items():
            if data["status"] == "OK":
                interval_str = str(data["interval"])
                constraint_id = self._stable_hash(doc_hash, param, interval_str)
                self.audit.log_constraint(
                    constraint_id=constraint_id,
                    parameter=param,
                    interval=data["interval"],
                    unit=data["unit"],
                    evidence_list=data["evidence"],
                    context=user_context or {},
                    steps=[{"type": "intersection", "input_hints": len(data["evidence"])}]
                )
            else:
                conflict_id = self._stable_hash(doc_hash, param, "CONFLICT")
                self.audit.log_conflict(
                    conflict_id=conflict_id,
                    parameter=param,
                    conflict_reason=data["conflict_reason"],
                    offending_evidence=data["offending_evidence"],
                    prior_evidence=data["prior_evidence"],
                    context=user_context or {}
                )
        return results
9. 测试策略
9.1 单元测试
覆盖率要求 > 85%，每个模块独立测试。

9.2 黄金集测试（三层门禁）
运行 pytest tests/test_golden_set.py --ci，100% 通过才允许合并。

9.3 集成测试
使用 Docker Compose 启动完整环境，验证端到端流程。

9.4 性能测试
目标：单次约束求解 < 2 秒（4070 CPU），并发 10 请求 P99 < 3 秒。

10. 风险与应对
风险	概率	影响	应对措施
版本管理混乱	中	高	使用 content_hash 唯一约束 + is_active
Solver 非确定性	低	高	第三排序键 + 单元测试验证
回调丢失	中	中	补偿任务 + 启动轮询
作用域匹配劣化召回	低	中	条件启用 scope 评分
存储爆炸	中	中	定期清理非活跃版本（MAX_VERSIONS_TO_KEEP）
11. 开发调试 UI 设计
为提升开发与调试效率，EKRS 提供一套基于 Web 的轻量级调试界面。该 UI 仅在开发模式（DEBUG_MODE=true）下启用，不部署至生产环境。

11.1 技术选型
组件	选型	说明
前端框架	Streamlit	快速搭建、与 Python 后端无缝集成，无需编写 HTML/CSS
后端对接	FastAPI 同一进程内调用	复用 DERE Pipeline 和 RAG 检索模块
数据展示	Pandas + Plotly	表格展示 hints、区间可视化
11.2 UI 功能模块
11.2.1 文档入库调试面板
目标：单步执行或批处理文档解析入库，实时查看中间产物。

功能点：

文件上传区域：支持拖拽上传 Markdown/PDF/Word 文件（PDF/Word 需先通过解析系统转换为 JSONL）。

解析参数配置：

doc_hash 自动计算或手动覆盖。

scope_path 根路径指定（如 ["GB150-2025"]）。

是否强制重新解析 (force_reparse)。

执行控制：

按钮：解析并入库。

进度条：展示解析 → 向量化 → 索引构建进度。

结果展示：

实时输出解析日志流。

提取到的 NumericHint 列表表格（可过滤、排序）。

入库后 Qdrant 中的 point 数量。

验证工具：

输入查询语句，立即测试该文档的召回效果。

11.2.2 约束查询调试面板
目标：逐层观察检索、提取、求解过程，定位门禁失败原因。

功能点：

查询输入区：

文本输入框（自然语言查询）。

结构化上下文编辑器（Key-Value 表格，可标记来源）。

严格模式开关。

作用域选择下拉框（从已入库文档自动提取）。

Replay 模式开关 + trace_id 输入框。

三层门禁透视：

召回层：展示 Qdrant 返回的 Top K chunks，高亮匹配关键词，显示相似度分数。

提取层：展示从 chunks 中提取的 NumericHint 原始列表，标记哪些被作用域过滤、哪些被单位归一化。

求解层：展示区间交集运算的每一步（可视化数轴），冲突归因详情。

最终结果展示：

结构化 JSON 结果（带语法高亮）。

审计证据链卡片（点击可跳转至原始文档片段）。

一键复制：复制结果 JSON、cURL 命令、审计 Trace 数据。

11.2.3 黄金集验证面板
目标：快速运行黄金集测试并对比预期差异。

功能点：

加载 tests/golden_set.json 并显示用例列表。

单选或多选用例执行。

执行结果对比表格（预期 vs 实际，差异高亮）。

失败用例的详细诊断信息（哪一层门禁失败）。

11.3 UI 访问与安全
路由：/dev-ui（仅当 EKRS_DEBUG=true 时挂载）。

访问控制：本地开发默认 localhost 绑定；若需远程访问，必须配置 DEV_UI_TOKEN 进行简单 Bearer 认证。

性能限制：单次查询最大展示 hint 数量 500 条，防止前端卡死。

11.4 示例代码骨架（Streamlit）
python
# dev_ui/app.py
import streamlit as st
from ekrs.pipeline import DEREPipeline
from ekrs.retrieval import QdrantRetriever

st.set_page_config(page_title="EKRS 调试控制台", layout="wide")

tab1, tab2, tab3 = st.tabs(["📥 文档入库", "🔍 约束查询", "✅ 黄金集验证"])

with tab1:
    uploaded_file = st.file_uploader("上传规范文档 (Markdown/JSONL)")
    if uploaded_file and st.button("解析并入库"):
        with st.spinner("正在解析..."):
            # 调用解析流水线
            hints = pipeline.extract(uploaded_file.read().decode())
            st.success(f"提取到 {len(hints)} 条数值提示")
            st.dataframe(hints)

with tab2:
    query = st.text_input("查询语句")
    strict = st.checkbox("严格模式")
    if st.button("执行查询"):
        # 执行检索+求解
        result = retriever.query(query, strict=strict)
        st.json(result)
12. 开发阶段日志规范
为保证开发调试的可追溯性，EKRS 在非生产环境下输出详细调试日志（生产环境仅输出 INFO 及以上级别关键日志）。所有日志采用结构化 JSON 格式，统一输出至 stdout 或指定日志文件。

12.1 日志级别定义
级别	用途	示例场景
DEBUG	细粒度调试信息，包含中间变量值	提取的正则匹配详情、向量相似度分数
INFO	关键流程节点记录	文档入库开始/完成、求解成功
WARNING	潜在问题但不影响主流程	作用域无匹配回退到全局、单位无法识别
ERROR	可恢复的错误	回调失败重试、某个 chunk 解析异常
CRITICAL	系统不可用错误	数据库连接断开、Qdrant 无响应
12.2 日志通用字段
每条日志 JSON 必须包含以下基础字段：

json
{
  "timestamp": "2026-04-09T10:30:00.123Z",
  "level": "INFO",
  "module": "solver",
  "trace_id": "abc123",
  "message": "区间交集计算完成",
  "duration_ms": 12
}
12.3 各模块必须记录的日志内容
12.3.1 解析系统 (Parser)
事件	级别	必须记录的字段	示例值
开始解析文档	INFO	doc_hash, file_name, parser_version	"doc_hash": "sha256...", "file_name": "GB150.md"
提取 NumericHint 详情	DEBUG	hint_count, sample_hints (前3条)	"hint_count": 42, "sample_hints": [...]
写入 JSONL 完成	INFO	output_path, file_size_mb, duration_ms	"output_path": "/parsed_lib/...", "file_size_mb": 2.3
发送 RAG 通知	INFO	callback_url, rag_trace_id	"callback_url": "http://rag:8000/v1/ingestion/notify"
通知失败重试	WARNING	retry_count, error	"retry_count": 2, "error": "Connection refused"
心跳检测超时	ERROR	doc_hash, last_heartbeat	"doc_hash": "abc", "last_heartbeat": "2026-04-09T10:00:00Z"
版本清理执行	INFO	deleted_versions, kept_versions	"deleted_versions": [1,2], "kept_versions": [3]
12.3.2 RAG 服务入库流程
事件	级别	必须记录的字段
接收通知请求	INFO	doc_hash, version, trace_id
开始向量化	INFO	chunk_count
向量化单条 chunk 详情	DEBUG	chunk_id, embedding_dim, sparse_dim
写入 Qdrant 完成	INFO	point_count, duration_ms
回调解析系统	INFO	callback_url, rag_status
回调失败重试	WARNING	retry_count, status_code
补偿任务扫描	INFO	scanned_tasks, fixed_tasks
12.3.3 约束查询与求解
事件	级别	必须记录的字段
收到查询请求	INFO	query, strict, context_keys, top_k
检索阶段	DEBUG	retrieved_chunk_ids, scores (前5)
作用域匹配	DEBUG	active_scope, filtered_hint_count
提取 NumericHint 列表	DEBUG	total_hints, per_param_counts
求解过程（每步交集）	DEBUG	param, current_interval, next_interval, applied_hint
冲突检测	WARNING	param, offending_hint, prior_hints
求解完成	INFO	param, final_interval, evidence_count, duration_ms
门禁失败	ERROR	gate (recall/extract/solve), reason, partial_data
严格模式拒绝	ERROR	missing_context_params
12.3.4 Replay 模式
事件	级别	必须记录的字段
Replay 请求	INFO	replay_trace_id
历史 hints 加载	INFO	loaded_hints_count, cached_at
缓存未命中	ERROR	trace_id, message
12.4 审计日志与调试日志分离
审计日志（第8.3节）永久记录每一次求解的证据与结论，用于合规审查，写入专用 audit.log，不可关闭。

调试日志（本节）记录系统内部状态与性能数据，仅在开发阶段开启，可配置轮转策略（最大 100 MB，保留 5 个备份）。

12.5 日志配置示例（Python logging 与 python-json-logger）
python
import logging
from pythonjsonlogger import jsonlogger

# 调试日志配置
debug_logger = logging.getLogger("ekrs_debug")
handler = logging.StreamHandler()
formatter = jsonlogger.JsonFormatter(
    "%(timestamp)s %(level)s %(module)s %(trace_id)s %(message)s %(duration_ms)s"
)
handler.setFormatter(formatter)
debug_logger.addHandler(handler)
debug_logger.setLevel(logging.DEBUG if os.getenv("EKRS_DEBUG") == "true" else logging.INFO)
12.6 开发阶段日志分析建议
使用 jq 命令行工具快速过滤日志，例如：

bash
cat debug.log | jq 'select(.module == "solver" and .level == "DEBUG")'
利用 UI 面板（第11章）实时查看日志流，无需手动查文件。

附录：关键代码片段索引
功能	文件路径	说明
双哈希版本控制	db/task_repository.py	create_task、activate_version
幂等回调更新	db/task_repository.py	update_rag_status_idempotent
确定性排序	constraint_engine/solver.py	_constraint_sort_key
hint 提取含上下文	ingestion/numeric_hint_extractor.py	extract_hints_from_text
条件作用域重排序	retrieval/scorer.py	rerank_with_scope
Replay 模式	api/routes/constraints.py	检查 replay 参数
补偿任务	services/reconcile.py	ReconcileService
调试 UI 入口	dev_ui/app.py	Streamlit 应用
调试日志配置	core/logging.py	setup_debug_logging
13. 代码仓库目录结构
EKRS 仓库采用 Monorepo 结构，仅包含 RAG 服务核心、共享库、开发调试 UI 和部署配置。解析系统（Parser）为外部独立系统，通过 API 与本仓库交互，不包含在本仓库内。

text
ekrs/
├── .github/                          # CI/CD 配置
│   └── workflows/
│       ├── ci.yml                    # 主 CI 流水线
│       └── golden_set.yml            # 黄金集定期验证
│
├── docs/                             # 文档
│   ├── EKRS_开发规范_V2.3.md         # 本规范文档
│   ├── api/                          # OpenAPI 规范文件
│   │   └── openapi.yaml
│   └── parser_contract.md            # 解析系统接口契约（输入格式规范）
│
├── shared/                           # 共享 Python 库（rag 和 dev_ui 共同引用）
│   ├── ekrs_shared/
│   │   ├── __init__.py
│   │   ├── models.py                 # Pydantic 数据模型（NumericHint, Evidence 等）
│   │   ├── normalizer.py             # 单位归一化、参数同义词
│   │   ├── audit.py                  # 审计日志基类
│   │   └── utils.py                  # 工具函数（hash 计算等）
│   ├── pyproject.toml
│   └── README.md
│
├── rag/                              # RAG 服务（核心）
│   ├── ekrs_rag/
│   │   ├── __init__.py
│   │   ├── main.py                   # FastAPI 应用入口
│   │   ├── api/
│   │   │   ├── routes/
│   │   │   │   ├── constraints.py    # POST /v1/constraints
│   │   │   │   ├── ingestion.py      # POST /v1/ingestion/notify, GET /status
│   │   │   │   ├── metrics.py        # GET /metrics
│   │   │   │   └── replay.py
│   │   │   └── dependencies.py
│   │   ├── core/
│   │   │   ├── config.py
│   │   │   ├── logging.py            # 调试日志配置
│   │   │   └── metrics.py            # Prometheus 指标定义
│   │   ├── retrieval/
│   │   │   ├── qdrant_client.py      # Qdrant 封装
│   │   │   ├── embedder.py           # bge-m3 嵌入模型加载
│   │   │   ├── retriever.py          # 检索主逻辑
│   │   │   └── scorer.py             # 作用域重排序
│   │   ├── constraint_engine/
│   │   │   ├── models.py             # 内部约束模型
│   │   │   ├── normalizer.py         # 复用 shared 中的逻辑
│   │   │   ├── solver.py             # IntervalSolver
│   │   │   └── evidence_builder.py   # 构建证据链
│   │   ├── session/
│   │   │   └── context_manager.py    # 用户上下文管理
│   │   ├── audit/
│   │   │   └── audit_logger.py       # 审计日志写入
│   │   └── storage/
│   │       └── replay_cache.py       # Replay 模式缓存（Redis）
│   ├── tests/
│   │   ├── unit/
│   │   │   ├── test_solver.py
│   │   │   ├── test_normalizer.py
│   │   │   └── ...
│   │   ├── golden_set/
│   │   │   ├── golden_set.json       # 黄金集测试用例
│   │   │   └── test_golden_set.py
│   │   └── fixtures/
│   │       └── sample_hints.json
│   ├── Dockerfile
│   ├── pyproject.toml
│   └── README.md
│
├── dev_ui/                           # 开发调试 UI（Streamlit）
│   ├── app.py                        # UI 主入口
│   ├── pages/
│   │   ├── 01_📥_文档入库.py
│   │   ├── 02_🔍_约束查询.py
│   │   └── 03_✅_黄金集验证.py
│   ├── components/
│   │   ├── hint_table.py
│   │   └── interval_viz.py
│   ├── utils/
│   │   └── api_client.py             # 调用本地 RAG 服务
│   └── requirements.txt
│
├── deployment/                       # 部署配置
│   ├── docker-compose.yml            # 本地开发环境（含 Qdrant、Redis、RAG）
│   ├── docker-compose.ci.yml         # CI 环境
│   ├── k8s/                          # Kubernetes 部署清单
│   │   ├── rag/
│   │   ├── qdrant/
│   │   └── redis/
│   └── grafana/
│       └── dashboards/
│           └── ekrs.json
│
├── scripts/                          # 运维与工具脚本
│   ├── ci_gate.sh                    # CI 门禁脚本
│   ├── run_golden_set.sh
│   └── mock_parser_notify.sh         # 模拟解析系统通知（测试用）
│
├── .env.example                      # 环境变量模板
├── .gitignore
├── Makefile                          # 常用命令快捷方式
└── README.md                         # 项目整体说明
13.1 解析系统（外部）接口契约
解析系统作为外部依赖，必须遵循以下规范与 EKRS 交互：

项目	规范
输出格式	JSONL（每行一个 JSON 对象），符合 DocumentBlock IR 定义
存放路径	共享存储（如 NFS），路径通过 /v1/ingestion/notify 的 output_path 告知 EKRS
通知接口	POST /v1/ingestion/notify（见 5.1 节）
回调接口	解析系统必须实现 POST /v1/callback 供 EKRS 回传状态（见 5.2 节）
认证	通过 X-Parser-Token Header 传递共享密钥
详细的解析系统输出格式规范定义在 docs/parser_contract.md 中（可独立维护）。

13.2 关键路径映射（与规范章节对应）
规范章节 / 功能	对应代码路径
4.2 Pydantic 数据模型	shared/ekrs_shared/models.py
5.1 通知接口（RAG 接收）	rag/ekrs_rag/api/routes/ingestion.py
5.3 约束查询 API	rag/ekrs_rag/api/routes/constraints.py
8.1 单位归一化	shared/ekrs_shared/normalizer.py
8.2 求解器核心	rag/ekrs_rag/constraint_engine/solver.py
8.3 审计日志	rag/ekrs_rag/audit/audit_logger.py
11. 开发调试 UI	dev_ui/
12. 日志配置	rag/ekrs_rag/core/logging.py
13.3 依赖管理说明
shared 包作为可编辑依赖安装：

bash
# 在 rag 和 dev_ui 目录下执行
pip install -e ../shared
生产镜像构建时，将 shared/ 复制到容器内并安装。

13.4 Makefile 常用命令示例
makefile
# Makefile
.PHONY: dev test lint clean

dev:
	docker-compose -f deployment/docker-compose.yml up -d
	cd rag && uvicorn ekrs_rag.main:app --reload --port 8000 &
	cd dev_ui && streamlit run app.py

test:
	cd rag && pytest tests/

golden:
	cd rag && pytest tests/golden_set/test_golden_set.py --ci

lint:
	flake8 shared rag
	mypy shared rag

mock-notify:
	./scripts/mock_parser_notify.sh
## 14. 依赖清单

### 14.1 Python 依赖

#### RAG 服务 (`rag/pyproject.toml` 或 `requirements.txt`)
fastapi==0.115.0
uvicorn[standard]==0.30.0
pydantic==2.8.0
qdrant-client==1.11.0
httpx==0.27.0
tenacity==8.5.0
redis==5.0.1
aiosqlite==0.20.0
prometheus-client==0.19.0
python-json-logger==2.0.7
portion==2.4.0
numpy==1.26.0 # bge-m3 依赖
onnxruntime==1.18.0 # bge-m3 推理
FlagEmbedding==1.2.0 # 可选，用于加载 bge-m3

text

#### 共享库 (`shared/pyproject.toml`)
pydantic==2.8.0
portion==2.4.0

text

#### 开发 UI (`dev_ui/requirements.txt`)
streamlit==1.35.0
pandas==2.2.0
plotly==5.22.0
httpx==0.27.0

text

### 14.2 系统依赖（Docker 镜像基础）

- **基础镜像**：`python:3.11-slim-bookworm`
- **额外 apt 包**：
build-essential curl git
libopenblas-dev libomp5 # onnxruntime 运行时依赖
## 15. 部署拓扑与网络架构

### 15.1 开发环境拓扑

┌─────────────────────────────────────────────────────────┐
│                     Docker Network: ekrs-net            │
├─────────────────────────────────────────────────────────┤
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐  │
│  │   RAG API   │    │   Qdrant    │    │    Redis    │  │
│  │   :8000     │───▶│   :6333/4   │    │   :6379     │  │
│  └──────┬──────┘    └─────────────┘    └─────────────┘  │
│         │                                               │
│         │ 共享存储卷                                     │
│         ▼                                               │
│  ┌─────────────────────────────────────────────────┐    │
│  │            parsed_lib (NFS / Bind Mount)        │    │
│  │    由外部解析系统写入，RAG 服务只读              │    │
│  └─────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────┘
        ▲
        │ HTTP :8000 (内部)
        │
┌───────┴───────┐    ┌───────────────┐
│   Dev UI      │    │  外部解析系统  │
│   :8501       │    │  (独立部署)    │
└───────────────┘    └───────┬───────┘
                             │ 通知: POST /notify
                             ▼
                    RAG API :8000
15.2 端口分配
服务	端口	用途	对外暴露
RAG API	8000	约束查询、入库通知、状态查询	是（内部/网关）
Qdrant	6333 (HTTP), 6334 (gRPC)	向量数据库	仅内部
Redis	6379	分布式锁、缓存	仅内部
Dev UI	8501	调试界面	仅开发环境
## 16. 安全规范

### 16.1 服务间认证

- **解析系统 → RAG**：HTTP Header `X-Parser-Token: <shared_secret>`，密钥通过环境变量 `PARSER_TOKEN` 注入，长度至少 32 字符。
- **RAG → 解析系统回调**：同样使用 `X-Parser-Token`。

### 16.2 用户认证（可选）

- 生产环境建议在 RAG API 前放置 API 网关（如 Nginx、Kong），由网关负责 JWT 验证或 API Key 验证。
- 本服务本身不实现用户体系，仅透传 `trace_id`。

### 16.3 敏感信息保护

- 所有密钥通过 Docker Secrets 或 Kubernetes Secrets 挂载，禁止写入镜像或代码仓库。
- 审计日志中不得记录 `X-Parser-Token` 明文。

### 16.4 CORS 配置

- 开发模式：允许所有来源。
- 生产模式：仅允许白名单内的前端域名。
## 17. 错误码参考

### 17.1 HTTP 状态码

| 状态码 | 含义 | 示例场景 |
| :--- | :--- | :--- |
| 200 | 成功 | 查询成功 |
| 202 | 已接受 | 入库通知排队中 |
| 400 | 请求参数错误 | 缺少 query 字段、strict=true 缺上下文 |
| 403 | 认证失败 | X-Parser-Token 无效 |
| 404 | 资源不存在 | replay trace_id 不存在 |
| 409 | 冲突 | 同一 content_hash 已存在活跃版本 |
| 500 | 内部错误 | 求解器异常、数据库连接失败 |

### 17.2 业务错误码（响应体中的 `error` 字段）

| error | 说明 | 附加字段 |
| :--- | :--- | :--- |
| `missing_context` | 严格模式下缺少必要上下文 | `required_params` |
| `invalid_trace_id` | replay 模式提供的 trace_id 无效 | - |
| `conflict_detected` | 求解时发现约束冲突 | `conflict_details` |
| `no_hints_found` | 未从文档中提取到相关数值提示 | - |
## 18. 配置模板

### 18.1 `.env.example` 完整内容

```bash
# ===== 通用配置 =====
PARSER_TOKEN=change-me-to-32-char-random-string
RAG_BASE_URL=http://localhost:8000
SHARED_STORAGE_PATH=/parsed_lib
LOG_LEVEL=INFO
EKRS_DEBUG=false

# ===== 版本控制 =====
MAX_VERSIONS_TO_KEEP=3
HEARTBEAT_INTERVAL_SECONDS=300
ZOMBIE_TIMEOUT_MINUTES=30

# ===== 回调 =====
PARSER_CALLBACK_BASE=http://parser:8000

# ===== Redis =====
REDIS_URL=redis://localhost:6379
RECONCILE_INTERVAL_SECONDS=600

# ===== 检索与作用域 =====
SCOPE_MATCH_WEIGHT=0.15
FORKING_ENABLED=true
STRICT_MODE_DEFAULT=false
TOP_K_DEFAULT=40

# ===== Qdrant =====
QDRANT_HOST=localhost
QDRANT_GRPC_PORT=6334
COLLECTION_NAME=rag_documents
## 19. 核心流程时序图

### 19.1 文档入库流程
外部解析系统 RAG 服务 Qdrant Redis
│ │ │ │
│ POST /notify │ │ │
│──────────────────▶│ │ │
│ │ 获取分布式锁 │ │
│ │─────────────────────────────────────▶│
│ │ │ │
│ │ 读取 JSONL，向量化 │ │
│ │───────────┐ │ │
│ │ │ │ │
│ │◀──────────┘ │ │
│ │ │ │
│ │ 批量写入 points │ │
│ │───────────────────▶│ │
│ │ │ │
│ │ 释放锁 │ │
│ │─────────────────────────────────────▶│
│ │ │ │
│ │ POST /callback │ │
│◀──────────────────│ │ │

text

### 19.2 约束查询流程
客户端 RAG API 检索模块 求解器
│ │ │ │
│ POST /constraints │ │ │
│───────────────────▶│ │ │
│ │ 语义检索 │ │
│ │──────────────────▶│ │
│ │ │ 向量搜索 │
│ │ │────────┐ │
│ │ │ │ │
│ │ │◀───────┘ │
│ │ 返回 chunks │ │
│ │◀──────────────────│ │
│ │ │ │
│ │ 提取 NumericHint │ │
│ │─────────────────────────────────────▶│
│ │ │ │
│ │ │ 区间求解 │
│ │ │ (含冲突检测) │
│ │◀─────────────────────────────────────│
│ │ │ │
│ 结构化结果 │ │ │
│◀───────────────────│ │ │

## 20. 文档维护

- 本规范文档存放于 `docs/EKRS_开发规范_V2.x.md`，随代码仓库版本迭代更新。
- 重大变更（API 不兼容、铁律修改）需升级主版本号（V3.0），并经过架构组评审。
- 每次更新必须同步更新版本历史表格。
- 接口契约变更需同步更新 `docs/api/openapi.yaml`。
1. 幂等回调更新 SQL 片段
python
# db/task_repository.py
async def update_rag_status_idempotent(
    conn: aiosqlite.Connection,
    doc_hash: str,
    version: int,
    rag_status: str,
    rag_trace_id: str
) -> bool:
    """幂等更新 RAG 状态，仅当当前状态为空或非 success 时生效"""
    cursor = await conn.execute("""
        UPDATE parse_tasks
        SET rag_status = ?,
            rag_trace_id = ?,
            rag_updated_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP
        WHERE doc_hash = ?
          AND version = ?
          AND (rag_status IS NULL OR rag_status != 'success')
    """, (rag_status, rag_trace_id, doc_hash, version))
    await conn.commit()
    return cursor.rowcount > 0
2. 条件作用域重排序核心公式
python
# retrieval/scorer.py
def rerank_with_scope(
    chunks: List[Chunk],
    query_scope_terms: Optional[List[str]],
    scope_weight: float = 0.15
) -> List[Chunk]:
    """当 query_scope_terms 非空时，在语义分基础上增加作用域匹配奖励"""
    if not query_scope_terms:
        return chunks

    for chunk in chunks:
        scope_bonus = 0.0
        if chunk.scope_path:
            # 计算 chunk 作用域与查询作用域的重叠度
            overlap = len(set(chunk.scope_path) & set(query_scope_terms))
            scope_bonus = overlap * scope_weight
        chunk.final_score = chunk.semantic_score + scope_bonus

    return sorted(chunks, key=lambda c: c.final_score, reverse=True)
3. Replay 模式缓存存取
python
# storage/replay_cache.py
import json
from redis import Redis

class ReplayCache:
    def __init__(self, redis_client: Redis, ttl_days: int = 7):
        self.redis = redis_client
        self.ttl = ttl_days * 86400

    def save(self, trace_id: str, hints: List[dict], context: dict):
        key = f"replay:{trace_id}"
        data = json.dumps({"hints": hints, "context": context, "ts": time.time()})
        self.redis.setex(key, self.ttl, data)

    def load(self, trace_id: str) -> Optional[dict]:
        data = self.redis.get(f"replay:{trace_id}")
        return json.loads(data) if data else None
4. 补偿任务核心扫描逻辑
python
# services/reconcile.py
async def reconcile_orphan_tasks(db_path: str, rag_client):
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute("""
            SELECT doc_hash, version FROM parse_tasks
            WHERE status = 'processing'
              AND heartbeat < datetime('now', ?)
        """, (f'-{ZOMBIE_TIMEOUT_MINUTES} minutes',))
        rows = await cursor.fetchall()

        for doc_hash, version in rows:
            # 主动向 RAG 查询状态
            rag_status = await rag_client.get_status(doc_hash, version)
            if rag_status:
                await update_rag_status_idempotent(conn, doc_hash, version, rag_status, 'reconcile')
