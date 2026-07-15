工程知识恢复系统 (EKRS) 开发手册 V3.0
版本：V3.0
日期：2026-04-10
状态：正式发布
替代：V2.3 及所有 Patch

目录
背景与目标

铁律

整体架构

核心数据模型

API 接口规范

分阶段实施计划

技术栈明细与接口细化

DERE 核心实现

测试策略

风险与应对

开发调试 UI 设计

开发阶段日志规范

代码仓库目录结构

依赖清单

部署拓扑与网络架构

安全规范

错误码参考

配置模板

核心流程时序图

文档维护

附录：关键代码片段索引

1. 背景与目标
1.1 业务痛点
工程规范、标准、合同、管理规定中的约束（温度、压力、材料等）分散在大量非结构化文档中。传统 RAG 只能检索片段，无法：

提取结构化约束

处理单位换算

解决跨文档、跨版本冲突

区分正式、草案、过渡期、审阅意见等状态

工程师需要可追溯、可重现、高精度的约束答案，而非 LLM 幻觉。

1.2 系统目标
构建 工程知识恢复系统 (EKRS)：

从文档提取数值锚点 (NumericHint)，保留证据链

管理文档的时效性、权威性、条款演化链

通过纯函数式约束求解引擎计算参数可行区间

支持作用域感知、严格/推断模式、Replay

全链路可审计、可重现

1.3 系统边界
输入：解析系统输出的 JSONL（DocumentBlock IR）

输出：结构化约束、范围、单位、来源、冲突信息

不包含：LLM 自然语言生成、外部知识库自动更新、多模态识别

2. 铁律
编号	铁律	描述	验证方式
R1	证据化 Hint	每个 hint 必须包含 source_span、block_id、context_window	入库 payload 检查
R2	纯函数 Solver	引擎无 I/O、无状态、无副作用	单元测试确定性
R3	三层门禁	召回 → 提取 → 求解，全链路审计	黄金集测试
R4	显式优先级	User > Explicit_Doc > Inferred_Doc > Default	输出标注来源
R5	轻量 KG	仅 Entity Overlap 评分，无多跳推理	无图数据库
R6	严格模式	strict=true 时禁止推断，缺条件报错	API 测试
R7	作用域隔离	每个 hint 携带 scope_path	多分支输出测试
R8	索引层洁净	仅过滤非法状态，不裁剪权威性	Qdrant payload 检查
3. 整体架构
3.1 双层解耦架构图
text
┌─────────────────────────────────────────────────────────────┐
│                    上层业务系统 (EKRS Business)               │
│  - 文档管理 (状态/版本/演化链)                                │
│  - RAG 检索 + Hint 提取                                      │
│  - 业务策略 → filters + priority 转换                        │
│  - 结果解释与审计                                            │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼ Constraint[] + filters + priority
┌─────────────────────────────────────────────────────────────┐
│                下层纯计算引擎 (EKRS Engine)                   │
│  - 泛化过滤 (meta 字段匹配)                                   │
│  - 泛化排序 (加权求和)                                       │
│  - 区间交集 (确定性)                                         │
│  - 覆盖层合并                                                │
│  - 冲突检测 (硬/软)                                          │
│  (无状态、无业务语义)                                         │
└─────────────────────────────────────────────────────────────┘
3.2 数据流向
外部解析器 → 共享存储 JSONL → 通知业务层

业务层读取 → 分块 → 向量化 → Qdrant

用户查询 → 业务层检索 → Hint 提取 → 条款演化解析

Hint → Constraint IR V2 → 调用引擎

引擎返回区间 → 业务层解释 → 返回用户

4. 核心数据模型
4.1 文档元数据表 documents
sql
CREATE TABLE documents (
    doc_hash TEXT PRIMARY KEY,
    title TEXT,
    series_id TEXT,
    authority TEXT NOT NULL,
    effective_date DATE NOT NULL,
    status TEXT NOT NULL,
    replaced_by TEXT,
    parent_series_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
4.2 文档替代关系表 doc_supersedes
sql
CREATE TABLE doc_supersedes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    superseding_doc_hash TEXT NOT NULL,
    superseded_doc_hash TEXT NOT NULL,
    effective_date DATE NOT NULL,
    transition_end_date DATE,
    reason TEXT,
    UNIQUE(superseding_doc_hash, superseded_doc_hash)
);
4.3 条款覆盖关系表 provision_overrides
sql
CREATE TABLE provision_overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_provision_id TEXT NOT NULL,
    target_doc_hash TEXT NOT NULL,
    source_provision_id TEXT NOT NULL,
    source_doc_hash TEXT NOT NULL,
    effective_from DATE NOT NULL,
    effective_until DATE,
    reason TEXT,
    created_by TEXT,
    is_active BOOLEAN DEFAULT TRUE,
    override_chain_id TEXT
);
4.4 Constraint IR V2（RFC 级）
typescript
interface Constraint {
  id: string;
  parameter: string;
  interval: {
    lower: number | "-inf";
    upper: number | "inf";
    lower_open: boolean;
    upper_open: boolean;
  };
  unit?: string;

  lifecycle: {
    status: "active" | "draft" | "transitional" | "review" | "deprecated";
    effective_from?: string;
    effective_until?: string;
    is_binding: boolean;
  };

  source: {
    doc_hash: string;
    provision_id?: string;
    authority_score: number;
  };

  priority: {
    explicit_level: number;      // User > Doc > Inferred (100/80/60)
    recency_score: number;
    authority_score: number;
  };

  scope?: {
    path: string[];
    conditions?: Record<string, any>;
  };

  evidence: {
    text_span: string;
    block_id: string;
  };

  inferred: boolean;
}
JSON Schema 见附录。

5. API 接口规范
5.1 业务层 API
5.1.1 约束查询 POST /v1/constraints
请求：

json
{
  "query": "高温环境下温度限制",
  "context": { "material": "Q345" },
  "scope": "ACTIVE_ONLY",
  "policy": "CONSERVATIVE",
  "overlay_hints": [...],
  "strict": false,
  "top_k": 40
}
响应：

json
{
  "trace_id": "...",
  "mode": "single",
  "parameters": {
    "temperature": { "range": [50, 80], "unit": "C" }
  },
  "applied_context": {...},
  "warnings": []
}
5.1.2 条款追溯 POST /v1/constraints/trace
返回指定 provision_id 在所有状态文档中的历史值。

5.2 引擎 API POST /v1/calculate
请求：包含 Constraint[]、filters、priority、overlay（详见第 8 章）。

6. 分阶段实施计划
阶段	内容	交付物	验收标准
Phase 1	基础底座：文档表、分块入库、Qdrant 集成	业务层骨架、通知接口	文档成功索引，状态正确推断
Phase 2	约束求解核心：Hint 提取、引擎 v1、黄金集	引擎服务、IR V2 适配	确定性求解，严格模式生效
Phase 3	作用域感知与多分支	作用域重排序、多分支输出	高温/一般工况分支正确
Phase 4	系统集成：回调幂等、补偿任务、分布式锁	完整闭环	并发安全，状态最终一致
Phase 5	可观测性：Prometheus、审计日志、CI 门禁	监控面板、Replay	指标可抓取，CI 阻断
	- 5.5 D: Prometheus sidecar exporter (:9090, prometheus_client multiprocess)
	- 5.5 E: 路由依赖注入（FastAPI Depends），删除模块级 set_X 单例
	- 5.5 F: 审计日志 rotation (100MB × 5 gzip) + /healthz 不写审计
Phase 6A	spec closure: 9 垂直切片补齐 (X-Admin-Key, DocumentRepo/A1, /trace, /calculate, soft fallback, golden 13→42, audit 2 fields, ENGINE_URL, 85% CI gate)	/api 路由 + audit 字段 + 测试 + CI	531 tests pass, 86.63% coverage, CI gate green
7. 技术栈明细与接口细化
组件	技术选型	用途
业务层	Python 3.11 + FastAPI	API、文档管理、RAG
向量数据库	Qdrant 1.11	dense + sparse 检索
嵌入模型	bge-m3 (ONNX, 1024d dense + sparse)	文本向量化(FlagEmbedding 框架)
关系数据库	aiosqlite / PostgreSQL	文档元数据、覆盖关系
缓存/锁	Redis 7	分布式锁、会话缓存
引擎	Python / Rust (可选)	纯计算服务
部署	Docker Compose / K8s	容器化编排
监控	Prometheus + Grafana	指标采集展示

### 7.4 首次部署与 dim 迁移流程(Phase 6B 新增)

Phase 6B 起,嵌入从 bge-small-en (384d) 切换到 bge-m3 (1024d + sparse),Qdrant 集合 dim 不匹配。
**首次部署**操作流程:

1. **启动 RAG 服务**:lifespan 自动检测 dim 不匹配(384d → 1024d),`AUTO_REINDEX=true`(默认)触发删旧集合+重建。等待服务 listen。
2. **触发 parser 全量重新推送**:parser 侧按文档清单逐个调 `POST /v1/ingestion/notify`,RAG 接收并 upsert(bge-m3 真实嵌入)。
3. **验证检索**:调 `POST /v1/constraints` 验证返回非空 + score 合理。
4. **监控**:首 24h 关注 `qdrant_write_failed` 审计事件(语义已放宽,见 §16)。

**生产部署**:`AUTO_REINDEX=false` 显式禁止 dim 自动重建,要求 operator 手动确认数据迁移窗口。

**AUTO_REINDEX=false 时的恢复步骤**(用户反馈点 3):
- 启动时 lifespan 抛 `RuntimeError: Collection ... dim=N does not match expected 1024.`
- 日志同时输出:`Set AUTO_REINDEX=true in .env to automatically rebuild the collection, OR manually delete and recreate it via Qdrant UI/API.`
- 运维人员选择:
  - (a) 临时方案:设 `AUTO_REINDEX=true`,重启服务(lifespan 重建)
  - (b) 永久方案:经业务方确认数据可重建后,通过 Qdrant REST API `DELETE /collections/rag_documents` + `POST /collections/rag_documents`(用 bge-m3 config)

8. DERE 核心实现
8.1 引擎核心逻辑（适配 IR V2）
python
def calculate(constraints, filters, priority, overlay):
    # 1. 过滤：上层通过 filters 控制 lifecycle 条件
    filtered = apply_filters(constraints, filters)
    # 2. 仅 is_binding=true 参与基线
    baseline = [c for c in filtered if c.lifecycle.is_binding]
    non_binding = [c for c in filtered if not c.lifecycle.is_binding]
    # 3. 按参数分组
    groups = group_by_parameter(baseline)
    results = {}
    for param, group in groups.items():
        sorted_group = apply_priority(group, priority)
        base_interval, evidence = intersect_all(sorted_group)
        if overlay:
            overlay_interval, _ = intersect_all(overlay.constraints)
            final = merge(base_interval, overlay_interval, overlay.mode)
        else:
            final = base_interval
        results[param] = {...}
    return results
8.2 刚性/弹性约束回退
python
def intersect_with_fallback(hard: List[Constraint], soft: List[Constraint]):
    hard_interval, _ = intersect_all(hard)
    if soft:
        soft_interval, _ = intersect_all(soft)
        merged = hard_interval & soft_interval
        if not merged.empty:
            return merged
        else:
            # 软约束与硬约束冲突，回退到硬约束，并记录警告
            return hard_interval, "soft_constraint_ignored"
    return hard_interval
8.3 严格模式处理
python
if strict:
    for c in baseline:
        if c.inferred:
            raise MissingContextError("Inferred constraint not allowed in strict mode")
9. 测试策略
单元测试：覆盖率 > 85%

黄金集测试：≥20 用例（Phase 6A 实测 42 cases: 13 legacy + 29 Phase 6A from `golden.md`），必须包含：

草案 vs 正式 (is_binding 过滤)

过渡期时间穿越 (effective_from/until)

strict 模式拒绝 inferred

硬冲突返回 409

单位换算边界 (MPa/psi, 开闭区间)

集成测试：Docker Compose 端到端

性能测试：单次求解 <2s，并发 10 P99 <3s

10. 风险与应对
风险	概率	影响	应对措施
状态元数据缺失	高	中	规则推断 + 人工确认队列 + 默认保守值
引擎非确定性	低	高	纯函数设计 + 确定性单元测试
回调丢失	中	中	补偿任务 + 启动轮询
大文档摄入超时	中	中	锁看门狗续约 + 分块处理
条款漂移无法自动映射	中	中	Fork 机制 + 人工对齐 UI
11. 开发调试 UI 设计
基于 Streamlit，功能：文档入库、约束查询、黄金集验证、覆盖关系管理。访问 http://localhost:8501。

12. 开发阶段日志规范
结构化 JSON 日志。关键新增字段：

lineage_snapshot：记录条款演化链（如 GB2011/5.3.2 → AMD1/5.3.2）

conflict_details：硬冲突时记录双方 provision_id 及原文片段

13. 代码仓库目录结构
text
ekrs/
├── rag/                      # RAG 服务（API + 检索 + 约束引擎 + 可观测性）
│   ├── ekrs_rag/             # 主包
│   │   ├── api/              # FastAPI 路由 + 中间件
│   │   ├── constraint_engine/  # IR V2 求解器（替代原独立 engine/）
│   │   ├── ingestion/        # 解析器通知 → 分块 → 向量化
│   │   ├── observability/    # Audit / Metrics / Trace
│   │   ├── retrieval/        # Qdrant 客户端
│   │   └── models.py / config.py / cli.py
│   └── tests/                # 单元、黄金集、集成（pytest）
├── shared/ekrs_shared/       # 共享 IR 模型 + 归一化 + 审计基类
├── dev_ui/                   # Streamlit 调试界面（占位，Phase 6 实施）
├── deployment/               # docker-compose.yml, prometheus.yml
├── docs/superpowers/         # 设计 spec + 实施 plan
└── scripts/                  # 运维脚本（mock_parser_notify.sh, load_golden_fixtures.py）
14. 依赖清单
运行时：fastapi, uvicorn, pydantic, qdrant-client, httpx, tenacity, redis,
aiosqlite, prometheus-client, python-json-logger, portion, onnxruntime,
transformers（bge tokenizer）
FlagEmbedding (==1.2.13) — bge-m3 dense+sparse 推理框架(Phase 6B 新增)
onnxruntime (>=1.15.0,<1.18.0) — FlagEmbedding 依赖(锁定避免 API drift)
numpy (>=1.24.0,<2.0.0) — FlagEmbedding 依赖(锁定 1.x API)
dev：pytest, pytest-asyncio, pytest-cov, fakeredis
注：streamlit 尚未安装（dev_ui/ 占位待 Phase 6 实施）；FlagEmbedding 未使用（实现选 onnxruntime + transformers 直接调用）。
15. 部署拓扑与网络架构
Docker Compose 编排 Qdrant、Redis、Engine、Business。生产环境通过 Ingress 暴露业务层 API。

RAG 服务暴露两个端口：
- 应用端口（默认 8000）：业务 API + `/healthz`
- 指标端口（默认 9090）：Prometheus sidecar exporter，通过 `METRICS_HOST` / `METRICS_PORT` 配置。`PROMETHEUS_MULTIPROC_DIR` 设置后启用多进程 collector（每个 worker 写 `.db` 文件，单一进程 bind 9090 端口）。

16. 安全规范
服务间 X-Parser-Token 认证

管理接口 X-Admin-Key 认证

敏感信息通过 Secrets 注入

**16 个事件名/schema 不可变更**:...(省略)... `qdrant_write_failed` (语义 Phase 6B 起放宽:覆盖 Qdrant 任何操作失败 read/write/delete/upsert/scroll,payload 含 `operation: str` 字段区分 read/write)。**back-compat 提示**:现有审计消费者(如监控脚本)需兼容 `operation` 字段缺失的情况——Phase 6A 之前的事件无此字段,Phase 6B 起的失败事件携带。监控脚本应:
- 处理新事件时优先用 `operation` 字段(若存在)
- 处理老事件时默认 `operation="write"`(Phase 6A 之前只有写入失败)
- 不要硬要求 `operation` 字段存在(用 `.get("operation", "write")`)

审计日志不记录令牌

审计日志 `audit.log` 永久保存，按 100 MB × 5 轮转（gzip 压缩，标准库 RotatingFileHandler）。`/healthz` 请求不写入审计（k8s 探活高频调用）。轮转后 AuditIndex 自动重建（仅扫描当前文件，跳过 `.gz` 历史）。**16 个事件名/schema 不可变更**（Phase 5: 15 个基线 + Phase 6A Task 2 注册 `document_metadata_failed` 孤儿事件）：constraint_solve_started/solved/failed, endpoint_started/completed, query_replay_executed, ingestion_received/completed/failed/replay_started/replay_completed/replay_sha256_mismatch, compensation_retry, qdrant_write_failed, lock_acquire_failed, document_metadata_failed。Phase 6A Task 4 新增 2 个可选字段 `lineage_snapshot` + `conflict_details`（不进入 required schema，通过 `_PHASE6A_OPTIONAL` 白名单透传）。

17. 错误码参考
HTTP	业务错误码	说明
400	missing_context	strict 模式缺必要上下文或存在 inferred
400	no_prior_solve	replay 但无历史 solve（先调 /v1/constraints）
400	incomplete_prior_solve	历史 solve 缺证据,无法 replay
400	replay_trace_id_required	replay 参数缺 trace_id
404	insufficient_recall	召回 chunk 数低于 MIN_RECALL_CHUNKS
404	no_constraints_extracted	提取门失败：chunk 内无 NumericHint
409	conflict	硬冲突（约束求解期,区间空集）
409	in_flight / not_completed / pre_phase5 / file_missing / sha256_mismatch	replay 请求被既有任务/状态/数据阻塞
422	invalid_ir / invalid_interval	IR 格式或区间非法（由 Pydantic validation 自动返回）
503	service_uninitialized	依赖（retriever/audit_index/pipeline/redis_lock/task_repo）未初始化
18. 配置模板
.env 包含 PARSER_TOKEN、ENGINE_URL（parser 回调地址，Phase 6A 补齐，Task 1）、QDRANT_HOST、QDRANT_GRPC_PORT、REDIS_URL、ADMIN_KEY（≥32 字符，管理接口 X-Admin-Key 认证）、AUDIT_LOG_PATH、TASKS_DB_PATH、DOCUMENTS_DB_PATH、METRICS_HOST、METRICS_PORT、PROMETHEUS_MULTIPROC_DIR（可选，启用多进程 Prometheus collector）等。CI 门禁：`pytest tests/ --cov=ekrs_rag --cov-fail-under=85`（Phase 6A 实测 86.63%）。

19. 核心流程时序图
文档入库：解析器 → 通知 → 业务层分块入库

约束查询：用户 → 业务层检索 → Hint 提取 → IR 转换 → 引擎计算 → 返回

20. 文档维护
存放于 docs/EKRS_开发手册_V3.0.md，重大变更升级主版本号。

21. 附录：关键代码片段索引
功能	路径	说明
Constraint IR V2 模型	shared/models/constraint.py	Pydantic 定义
约束构建器	business/adapter/constraint_builder.py	Hint → IR
引擎核心	engine/core/solver.py	过滤、排序、交集、回退
黄金集测试	tests/golden_set/	包含草案、过渡期等用例
4. 核心数据模型（更新）
4.5 Constraint IR V2（RFC 级正式规范）
typescript
interface Constraint {
  id: string;                          // 唯一标识

  parameter: string;                   // 规范化参数名，如 "temperature"

  value_type: "interval" | "enum" | "scalar" | "boolean";

  interval?: {
    lower: number | null;              // null 表示 -∞
    upper: number | null;              // null 表示 +∞
    lower_inclusive: boolean;
    upper_inclusive: boolean;
  };

  unit: string;                        // 归一化后的规范单位，如 "C", "MPa"

  conditions: Condition[];             // 适用条件（作用域）

  lifecycle: {
    status: "active" | "draft" | "transitional" | "review" | "deprecated";
    effective_date?: string;           // ISO 8601 日期
    expiry_date?: string;
    version?: string;
    is_binding: boolean;               // 是否具有约束力
  };

  source: {
    doc_id: string;
    provision_id?: string;
    doc_type: "standard" | "contract" | "review" | "draft";
    authority_score: number;           // 权威性数值
  };

  priority: {
    explicit_level: number;            // User(100) > Explicit_Doc(80) > Inferred_Doc(60) > Default(40)
    recency_score: number;             // 基于 effective_date 计算
    authority_score: number;
  };

  scope?: {
    path: string[];                    // 条款层级路径
    conditions?: Record<string, any>;
  };

  evidence: {
    text_span: string;                 // 原始文本片段
    block_id: string;
  };

  inferred: boolean;                   // 是否为推断产生
  confidence: number;                  // 提取置信度 (0-1)
}

interface Condition {
  field: string;
  operator: "=" | ">" | "<" | ">=" | "<=" | "!=";
  value: any;
}
JSON Schema 见附录 A。

8. DERE 核心实现（重构）
8.1 Constraint Builder 职责边界与确定性规则
Builder 定位：将 Hint 转换为 Constraint IR 的纯函数适配器。

绝对不做：

❌ 推理

❌ 多条规则合并

❌ 优先级裁决

❌ 版本选择

只做三件事：

结构化（Parse）：从文本中识别区间、运算符、条件。

标准化（Normalize）：参数名同义词映射、单位归一化。

生命周期标注（Lifecycle Tagging）：基于文档元数据和文本关键词标记状态。

8.1.1 模式识别规则表（L1）
文本模式	类型	示例	输出区间
X ≤ v ≤ Y	interval	50 ≤ T ≤ 80	[50, 80]
X between A and B	interval	temp between 10 and 20	[10, 20]
X ≥ A	lower bound	≥50	[50, +∞)
X ≤ B	upper bound	≤100	(-∞, 100]
X shall not exceed B	upper bound	不得超过80	(-∞, 80]
X shall be at least A	lower bound	至少50	[50, +∞)
X is Y	scalar	压力为 1.5MPa	[1.5, 1.5]
8.1.2 单位归一化规则（L3）
python
UNIT_MAP = {
    "°C": "C", "℃": "C", "Celsius": "C",
    "K": "K",
    "Pa": "Pa", "MPa": "MPa", "psi": "psi"
}

def normalize_unit(value: float, from_unit: str, to_unit: str) -> float:
    if from_unit == to_unit:
        return value
    # 温度特殊处理（仿射变换）
    if from_unit == "K" and to_unit == "C":
        return value - 273.15
    if from_unit == "C" and to_unit == "K":
        return value + 273.15
    # 压强乘性变换
    if from_unit == "MPa" and to_unit == "Pa":
        return value * 1_000_000
    if from_unit == "Pa" and to_unit == "MPa":
        return value / 1_000_000
    if from_unit == "psi" and to_unit == "Pa":
        return value * 6894.76
    raise ValueError(f"Unsupported conversion: {from_unit} -> {to_unit}")
8.1.3 生命周期推断规则（L5）
场景	触发条件	lifecycle.status	is_binding
征求意见稿	文件名或文本含 draft / 征求意见稿	draft	false
审阅意见	doc_type == "review" 或文本含 建议 / 审阅	review	false
过渡期标准	文本含 过渡期 或 transition period	transitional	true
正式生效	默认	active	true
已被替代	文档被新版本替代	deprecated	false
8.2 核心代码骨架（可直接实现）
python
import re
from datetime import date
from typing import Optional, Dict, Any, List

def build_constraint(hint: Dict[str, Any], doc_meta: Dict[str, Any]) -> Dict[str, Any]:
    # 1. 参数名标准化
    param = normalize_parameter(hint["parameter"])

    # 2. 区间解析
    interval = parse_interval(hint["text"])
    if interval is None:
        raise ValueError(f"Cannot parse interval from: {hint['text']}")

    # 3. 单位归一化
    canonical_unit = UNIT_MAP.get(hint.get("unit", ""), hint.get("unit", ""))
    if hint.get("unit") and canonical_unit != hint["unit"]:
        interval = convert_interval(interval, hint["unit"], canonical_unit)

    # 4. 生命周期标注
    lifecycle = infer_lifecycle(hint, doc_meta)

    # 5. 条件提取
    conditions = extract_conditions(hint["text"])

    # 6. 构建完整 IR
    return {
        "id": generate_constraint_id(hint),
        "parameter": param,
        "value_type": "interval",
        "interval": interval,
        "unit": canonical_unit,
        "conditions": conditions,
        "lifecycle": lifecycle,
        "source": {
            "doc_id": doc_meta["doc_hash"],
            "provision_id": hint.get("provision_id"),
            "doc_type": doc_meta.get("doc_type", "standard"),
            "authority_score": doc_meta.get("authority_score", 50)
        },
        "priority": {
            "explicit_level": 80,  # Explicit_Doc
            "recency_score": compute_recency_score(lifecycle.get("effective_date")),
            "authority_score": doc_meta.get("authority_score", 50)
        },
        "evidence": {
            "text_span": hint["source_text"],
            "block_id": hint["block_id"]
        },
        "inferred": hint.get("inferred", False),
        "confidence": hint.get("confidence", 0.9)
    }

def parse_interval(text: str) -> Optional[Dict[str, Any]]:
    # 50-80 或 50~80
    m = re.search(r'(\d+(?:\.\d+)?)\s*[-~]\s*(\d+(?:\.\d+)?)', text)
    if m:
        return {"lower": float(m.group(1)), "upper": float(m.group(2)),
                "lower_inclusive": True, "upper_inclusive": True}

    # >= 或 ≥
    m = re.search(r'(?:>=|≥|不小于|不低于)\s*(\d+(?:\.\d+)?)', text)
    if m:
        return {"lower": float(m.group(1)), "upper": None,
                "lower_inclusive": True, "upper_inclusive": False}

    # <= 或 ≤ 或 不超过/不大于/不得超过
    m = re.search(r'(?:<=|≤|不超过|不大于|不得超过)\s*(\d+(?:\.\d+)?)', text)
    if m:
        return {"lower": None, "upper": float(m.group(1)),
                "lower_inclusive": False, "upper_inclusive": True}

    # > 或 大于
    m = re.search(r'(?:>|大于|高于)\s*(\d+(?:\.\d+)?)', text)
    if m:
        return {"lower": float(m.group(1)), "upper": None,
                "lower_inclusive": False, "upper_inclusive": False}

    # < 或 小于
    m = re.search(r'(?:<|小于|低于)\s*(\d+(?:\.\d+)?)', text)
    if m:
        return {"lower": None, "upper": float(m.group(1)),
                "lower_inclusive": False, "upper_inclusive": False}

    return None

def infer_lifecycle(hint: Dict, doc_meta: Dict) -> Dict:
    text = hint.get("text", "").lower()
    doc_type = doc_meta.get("doc_type", "standard")

    if doc_type == "review" or "审阅" in text or "建议" in text:
        return {"status": "review", "is_binding": False}

    if "draft" in text or "征求意见稿" in text:
        return {"status": "draft", "is_binding": False}

    status = doc_meta.get("status", "active")
    return {
        "status": status,
        "effective_date": doc_meta.get("effective_date"),
        "version": doc_meta.get("version"),
        "is_binding": status in ("active", "transitional")
    }

def extract_conditions(text: str) -> List[Dict]:
    conditions = []
    # 提取“在...下”模式，简化处理
    m = re.search(r'在([^，,]+)环境下', text)
    if m:
        conditions.append({"field": "environment", "operator": "=", "value": m.group(1)})
    return conditions
9. 测试策略（补充边界用例）
9.2 黄金集必测用例（新增）
用例 ID	描述	输入 Hint	期望 IR
TC_DRAFT_01	征求意见稿状态识别	text: "温度不得超过80℃（征求意见稿）"	lifecycle.status = "draft", is_binding = false
TC_UNIT_01	开尔文转摄氏度	text: "温度 ≤ 300K"	interval.upper = 26.85, unit = "C"
TC_REVIEW_01	审阅意见识别	text: "建议将温度上限改为70℃"	lifecycle.status = "review", is_binding = false
TC_OPEN_01	开区间识别	text: "温度 > 50℃"	lower = 50, lower_inclusive = false
TC_TRANSITION_01	过渡期标准	doc_meta.status = "transitional"	lifecycle.status = "transitional", is_binding = true
TC_STRICT_01	严格模式拒绝推断	inferred = true, strict = true	返回 400 missing_context
TC_HARD_CONFLICT_01	硬冲突检测	[0,50] 与 [60,100]	返回 409 conflict
附录 A：Constraint IR V2 JSON Schema
json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "Constraint IR V2",
  "type": "object",
  "required": ["id", "parameter", "value_type", "lifecycle", "source"],
  "properties": {
    "id": { "type": "string" },
    "parameter": { "type": "string" },
    "value_type": { "enum": ["interval", "enum", "scalar", "boolean"] },
    "interval": {
      "type": "object",
      "properties": {
        "lower": { "type": ["number", "null"] },
        "upper": { "type": ["number", "null"] },
        "lower_inclusive": { "type": "boolean" },
        "upper_inclusive": { "type": "boolean" }
      }
    },
    "unit": { "type": "string" },
    "conditions": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "field": { "type": "string" },
          "operator": { "enum": ["=", ">", "<", ">=", "<=", "!="] },
          "value": {}
        }
      }
    },
    "lifecycle": {
      "type": "object",
      "required": ["status", "is_binding"],
      "properties": {
        "status": { "enum": ["active", "draft", "transitional", "review", "deprecated"] },
        "effective_date": { "type": "string", "format": "date" },
        "expiry_date": { "type": "string", "format": "date" },
        "version": { "type": "string" },
        "is_binding": { "type": "boolean" }
      }
    },
    "source": {
      "type": "object",
      "required": ["doc_id", "authority_score"],
      "properties": {
        "doc_id": { "type": "string" },
        "provision_id": { "type": "string" },
        "doc_type": { "enum": ["standard", "contract", "review", "draft"] },
        "authority_score": { "type": "number" }
      }
    },
    "priority": {
      "type": "object",
      "properties": {
        "explicit_level": { "type": "number" },
        "recency_score": { "type": "number" },
        "authority_score": { "type": "number" }
      }
    },
    "inferred": { "type": "boolean" },
    "confidence": { "type": "number", "minimum": 0, "maximum": 1 }
  }
}

