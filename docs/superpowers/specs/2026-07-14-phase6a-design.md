# Phase 6A — Spec Closure Design

> Spec closure for ekrs-handbook §4/§5/§8.2/§9/§12/§16/§18 gaps.
> 6A scope only. 6B (ops/dev_ui/load) is a separate future spec.

**Date:** 2026-07-14
**Phase tag target:** `phase6a-spec-closure`
**Iron Rules:** R1-R8 (含 R8) 维持不变

---

## §1 目标 / 非目标

**目标**:闭合 ekrs-handbook 中已声明但代码未实现的 9 项 spec 缺项,使 RAG 服务完全符合手册 §4-§18 的对外契约。

**包含**(9 项,均为 6A):
- §4 documents/doc_supersedes/provision_overrides 三张表
- §5 `/v1/constraints/trace` + `/v1/calculate` 两端点
- §8.2 硬约束空集时回退软约束的 `intersect_with_fallback`
- §9 golden set 13→20 例 + 覆盖率 78→85%
- §12 audit log 新增 `lineage_snapshot` + `conflict_details` 字段
- §16 X-Admin-Key 中间件
- §18 `.env.example` 添加 `ENGINE_URL`

**不含**(明确延后到 6B):
- dev_ui MVP、k8s 多副本、负载测试、Prometheus SLO/告警
- §9 性能基准 / benchmark
- 手册外新功能

**强约束**(不可破):
- 7 Iron Rules 不变(R1-R8,含 R8:Index 层仅过滤非法 status,不裁剪 authority)
- 15 个 audit 事件名/schema 不变,只追加 2 字段
- 已有 API 路径请求/响应契约不变(/trace、/calculate 为新增,非改造)
- 不引入新外部依赖
- 单 commit ≤500 LOC,review 闸门每项一次

---

## §2 14 项 spec gap 归位矩阵

| # | Spec § | 缺项 | 归位 | 理由 |
|---|--------|------|------|------|
| 1 | §4 | documents/doc_supersedes/provision_overrides 三表 | **6A** | ingestion notify 阶段 RAG 从 IR metadata 自动抽取写入(见 A1) |
| 2 | §5 | /v1/constraints/trace 端点 | **6A** | spec 声明,只读 audit log,可与 #1 并行(soft dep:文档表增强 `lineage_snapshot` 字段可读性) |
| 3 | §5 | /v1/calculate 端点(无 retrieve 直算) | **6A** | 独立功能,测试比 /trace 简单 |
| 4 | §8.2 | `intersect_with_fallback` 硬空回退软 | **6A** | 求解器语义补丁,影响 R6 strict 行为 |
| 5 | §9 | golden set 13→20 例 | **6A** | 4 完成后才能补黄金例 |
| 6 | §9 | 覆盖率 78→85% | **6A** | 1-5 实现后自然回升,补缺补到 85 |
| 7 | §12 | lineage_snapshot log 字段 | **6A** | 配合 1 文档表,审计可溯源 |
| 8 | §12 | conflict_details log 字段 | **6A** | 配合 4 软回退,审计可解释 |
| 9 | §16 | X-Admin-Key 中间件 | **6A** | admin scope 守护,2/3 都需 |
| 10 | §18 | .env.example 加 ENGINE_URL | **6A** | 一行配置,顺手 |
| 11 | §11 | dev_ui(Streamlit)空实现 | **6B** | 仅 dev,运维面,放 6B |
| 12 | §9 | 性能测试(并发/延迟) | **6B** | 需负载环境,运维属性 |
| 13 | §9 | benchmarks 基线 | **6B** | 同 12,基线依赖负载环境 |
| 14 | §9 | p95/p99 响应时间声明 | **6B** | 同 12,无负载无基线 |

**Iron Rules 影响**:
- R1(numeric_hint 必带 source_span/block_id/context_window):4 加 fallback 不影响
- R3(三闸门 recall→extract→solve):3 /calculate 复用同一闸门
- R6(strict 优先于软回退 — 见 D3)
- R7(scope_path 必带):不变
- R8(Index 层仅过滤 illegal):不变

**Accept / Defer**:14 项全部归入 6A(9)或 6B(5),无接受为废弃/重设计。9 项全部入 6A,无回填到 Phase 1-5(已 tag,不再重开)。

---

## §3 关键架构决策

### D1: X-Admin-Key 强制点
**采用 Depends(per-endpoint)**。
- 实现:`ekrs_rag/security.py` 新增 `require_admin_key` dependency
- header 名:`X-Admin-Key`
- env 变量:`ADMIN_KEY`
- env 缺失 → 端点返回 503 `admin_key_not_configured`(非跳过、非 500)

### D2: `intersect_with_fallback` 在求解器内的位置
**采用选项 A:放进 `IntervalSolver.solve()` 内部作为新分支**(改既有函数)。
- 新增私有方法 `_intersect_with_fallback(hard, soft)`
- `solve()` 接受 `allow_soft_fallback: bool` 参数
- §8.2 是求解器语义,放求解器层最自然

### D3: R6 strict 模式与软回退的优先级(已定)
**strict 优先于软回退**。
- `strict=true` → 软回退禁用,空硬约束 → 400 `strict_violation`
- `strict=false`(默认)+ `allow_soft_fallback=true`(默认)→ 软回退启用
- `strict=true` + `allow_soft_fallback=true` → 仍 400(strict 优先)
- R6 维持:strict 模式"缺上下文返回 400"语义不被软回退软化

### D4: 端点契约
- `POST /v1/constraints/trace` body:`{trace_id, scope_filter?}`
  - auth:普通(PARSER_TOKEN)
  - 响应:`{trace_id, events: [...], lineage_snapshot, conflict_details}`
- `POST /v1/calculate` body:`{constraints: [...], op, scope_path, strict, allow_soft_fallback?}`
  - `constraints`:与 `/v1/constraints` 的 `Constraint[]` 同 schema(Pydantic model 共享)
  - `op`:求解运算("intersect" 等,与 /v1/constraints 一致)
  - `scope_path`:必填,scope 路径(同 R7 规则)
  - `strict`:bool,默认 true
  - `allow_soft_fallback`:bool,默认 true
  - **auth:必须 admin(X-Admin-Key)**
  - 响应:与 /v1/constraints 同 envelope `{success, data: {branches: [...]}, error}`
- JSON,与现有 envelope 一致

### D5: 字段后向兼容
- audit log 追加 `lineage_snapshot: str | None` + `conflict_details: list | None` 为 **optional** 字段
- 已有事件 schema 不变,只追加 2 字段
- 新事件类型不新增(15 事件不变)

### D6: 覆盖率目标 85%
- 路径:每实现一项跑 pytest --cov 验证;末态再调个别薄覆盖文件做兜底
- 不为凑数写无意义测试,只补"已有功能但漏测"的真实路径

### D7: 文档表 / 审计字段落地方式
- 文档三表:新增 aiosqlite 表(与 TaskRepo 同库,`ekrs.db`),DDL `0006_documents.sql`
- lineage_snapshot / conflict_details:以 JSON 字符串(或 list)存 audit event kwargs(无需新表,审计是 append-only log)
- 文档表只存元数据(doc_id, type, created_at, scope_path, status),向量仍在 Qdrant

---

## §4 组件 & 数据流

### 新增文件

| 路径 | 用途 |
|------|------|
| `rag/ekrs_rag/security.py` | `require_admin_key` Depends + `verify_admin_key` helper |
| `rag/ekrs_rag/db/documents.py` | `DocumentRepo`(aiosqlite):documents / doc_supersedes / provision_overrides 三表 CRUD |
| `rag/ekrs_rag/db/migrations/0006_documents.sql` | 3 张表 + 索引 DDL |
| `rag/ekrs_rag/api/v1/trace.py` | `/v1/constraints/trace` 端点 |
| `rag/ekrs_rag/api/v1/calculate.py` | `/v1/calculate` 端点 |
| `rag/tests/unit/security/test_admin_key.py` | 鉴权 4 例 |
| `rag/tests/unit/db/test_documents_repo.py` | DocumentRepo CRUD + supersede/override 联表 4 例 |
| `rag/tests/unit/api/test_trace.py` | /trace 4 例 |
| `rag/tests/unit/api/test_calculate.py` | /calculate 5 例 |
| `rag/tests/unit/solver/test_fallback.py` | intersect_with_fallback 6 例 |
| `rag/tests/integration/test_phase6_e2e.py` | 端到端 2 例 |
| `rag/tests/fixtures/golden/v2/*.json` | 新增 7 例黄金样例 |

### 修改文件

| 路径 | 改动 |
|------|------|
| `rag/ekrs_rag/constraint_engine/solver.py` | `IntervalSolver.solve()` 加 `allow_soft_fallback`;新私有 `_intersect_with_fallback()` |
| `rag/ekrs_rag/api/v1/constraints.py` | `SolveRequest` 加 `allow_soft_fallback: bool = True`;透传 solver |
| `rag/ekrs_rag/observability/audit.py` | 15 事件 schema 各加 `lineage_snapshot` + `conflict_details`(可空) |
| `rag/ekrs_rag/main.py` | lifespan:跑 0006 迁移;注册 /trace、/calculate router |
| `rag/ekrs_rag/api/dependencies.py` | 加 `get_document_repo` Depends |
| `shared/ekrs_shared/audit.py` | `AuditLogger.log_event` kwargs 白名单扩 2 字段(可选) |
| `rag/tests/fixtures/golden/golden_set.json` | 索引文件 13→20 |
| `ekrs-handbook.md` | §4/§5/§8.2/§9/§12/§16/§18 各节随对应 commit 改 |
| `.env.example` | 加 `ADMIN_KEY=` + `ENGINE_URL=http://localhost:8000` |
| `pyproject.toml` (rag/) | 不加新依赖 |

### 数据流(/v1/constraints/trace)
```
client → POST /trace{trace_id} → AuditIndex.seek(trace_id)
  → 读 audit.log 偏移 → 解析该 trace 事件序列
  → 过滤 scope(可选) → 返回 {trace_id, events, lineage_snapshot, conflict_details}
```
只读 audit log,无新写。**lineage_snapshot / conflict_details 对老 trace 返回 null**(6A 才加的字段,旧条目无值)。

### 数据流(/v1/calculate)
```
admin client → POST /calculate{constraints, op, scope_path, strict, allow_soft_fallback}
  → require_admin_key ✓
  → 构造 IR 直送求解器(无 Qdrant 检索)
  → solver.solve(allow_soft_fallback=..., strict=...)
    → 硬约束 ∩ → 空 ∧ allow_soft ∧ ¬strict → 软约束 ∩
  → 审计:solve 事件 + lineage_snapshot(= 输入约束快照) + conflict_details(= 软回退标记)
  → 返回 {branches: [...], lineage_snapshot, conflict_details}
```

### 数据流(ingestion 写入文档表 — A1 决议)
```
parser → POST /v1/ingestion/notify{IR, doc_metadata}
  → 现有 ingestion flow 处理 IR
  → 新增:从 IR.doc_metadata 抽取 doc_id, type, scope_path, status
  → DocumentRepo.insert(documents)
  → 跨文档 supersede/override 时:DocumentRepo.link_supersede/override
```
RAG 侧负责写入,parser 提供 metadata 字段(A1 决议,见 §7)。

### 错误处理
- 401 missing/bad X-Admin-Key(/calculate、admin scope 端点)
- 503 `ADMIN_KEY` 未配置(守护已开但缺配置)
- 422 输入约束非法(Pydantic 自动)
- 400 strict=true 触发软回退场景(明文 `strict_violation` 错误)
- 注:/trace 读 audit.log 不依赖 Qdrant,无 Qdrant 不可达分支

### Iron Rules 兼容
D3 已说明 strict 优先于软回退,R6 维持。

---

## §5 测试策略

### 单元测试新增(共 23 例)

| 模块 | 例数 | 覆盖目标 |
|------|------|----------|
| `test_admin_key.py` | 4 | 缺/错/对 token + 未配置 503 |
| `test_documents_repo.py` | 4 | insert/get/supersede/override CRUD |
| `test_trace.py` | 4 | 无 token/有 token/无 trace_id/正常追溯 |
| `test_calculate.py` | 5 | strict/allow_soft/无 admin/有 admin/空硬回退 |
| `test_fallback.py` | 6 | 硬空/部分硬/全硬/strict 拒/非 strict 允/无软约束时仍空 |
| **小计** | **23** | |

### 黄金集扩充 13 → 20(新增 7 例)
1. 硬约束空 + 软约束有 + 非 strict → 软回退返回软结果
2. 硬约束空 + 软约束有 + strict → 400 strict_violation
3. /calculate 无 admin → 401
4. /calculate 缺 ENGINE_URL 配置 → 503
5. /trace 不存在 trace_id → 空 events 列表
6. /trace 跨 scope 过滤 → 仅返回匹配 scope 事件
7. 文档被 supersede 后 lineage_snapshot 反映被替换关系

### 覆盖率路径 78% → 85%
- 求解器(solver.py):fallback 分支 6 例 → +3-4%
- audit(audit.py):lineage_snapshot/conflict_details 写路径 → +1%
- security.py / documents.py / trace.py / calculate.py:新文件 100% → +2-3%
- 兜底:末态跑 `--cov=ekrs_rag --cov-report=term-missing`,识别 <85% 文件,补真实缺测路径

### 集成测试(`tests/integration/test_phase6_e2e.py`, 2 例)
- 端到端:parser notify → ingestion 落库 → /calculate 直算(无 Qdrant)→ 审计可查
- 端到端:同 IR + /v1/constraints(走 Qdrant)结果与 /calculate 一致(验证 solver 复用)

### 回归保护
- Iron Rules 测试(`tests/contract/test_iron_rules.py` 已存在)不需改
- 15 事件 schema 兼容性:`tests/observability/test_audit_event_registry.py` 验证 schema 集未变
- 后向兼容:旧请求(无 `allow_soft_fallback`)走默认 True,Pydantic 默认值保障

### 性能测试
**不在 6A**(归 6B)。6A 不引入 perf 断言。

### Iron Rules 回归重点
- R6:strict=true + 触发回退场景 → 必须在 6A 测试中显式断言(`test_calculate.py` 第 2 例 + `test_fallback.py` 第 4 例)

---

## §6 迁移 & 部署

### Schema 迁移
- 路径:`rag/ekrs_rag/db/migrations/0006_documents.sql`
- DDL:3 张表 + 索引
  - `idx_documents_scope_path`
  - `idx_documents_status`
  - `idx_doc_supersedes_from`、`idx_doc_supersedes_to`
  - `idx_provision_overrides_scope_path`
- 执行点:app lifespan 启动时(SQLite `IF NOT EXISTS` 幂等)
- 回滚:无需,新增表,空表无副作用
- 与 IR 流解耦:文档表只存元数据,向量仍在 Qdrant

### 配置变更(`.env.example` 增量)
```bash
# 新增
ADMIN_KEY=                          # 留空 = /calculate 端点返回 503
ENGINE_URL=http://localhost:8000    # parser 回调地址
```
- 已有变量不动
- 缺 `ADMIN_KEY` 行为:startup WARN 日志;`/calculate` 端点返回 503 `admin_key_not_configured`
- 缺 `ENGINE_URL` 行为:已在 ingestion flow 处理,6A 仅补文档

### 后向兼容
- API:旧请求体(无 `allow_soft_fallback`)→ Pydantic 默认 True,无破坏
- 响应:无字段删除,`lineage_snapshot` / `conflict_details` 为可空,旧调用方忽略
- 路由:仅新增,无修改
- audit log:schema 只追加 2 可选字段,旧日志条目继续可读

### 部署顺序
- 9 项独立 commit,无 deploy 协调需求
- 每项 commit 后单服务可重启,新增端点未注册前 404(正常)
- 文档表 0006 迁移 idempotent,重启可重入
- 无需 feature flag

### 版本 & 标签
- 9 项 commit 完成后打 tag `phase6a-spec-closure`
- 不发版本号(项目无 semver 流程,只打 tag)
- handbook 更新随 9 项 commit 同步

### 风险评估
- §8.2 求解器行为变更:R6 strict 守护 + 6 单测 + 黄金例覆盖 → 低
- §16 X-Admin-Key 缺配 503:行为显式,startup WARN 提示 → 低
- §12 audit 字段追加:append-only log,旧条目不受影响 → 极低
- §4 新表:空表,无数据迁移 → 极低

### 回滚
- 每项独立 commit,git revert 即可
- 文档表 0006 迁移无 destructive 操作,无回滚脚本

---

## §7 未解决问题

### 已决(本 spec 中固化)
- ✅ R6 strict 优先于软回退 — D3 锁定
- ✅ /v1/calculate 必须 admin — D4 锁定
- ✅ ADMIN_KEY 缺配 → 503 — D1 锁定

### 待解(不阻塞 spec,实施时定)
- ✅ 4: 文档表由 RAG 在 ingestion notify 阶段从 IR.doc_metadata 抽取写入(见 §4 数据流)— A1 决议
- ❓ 5: `lineage_snapshot` 字段格式(JSON 字符串 vs 结构化对象)— 实施时定
- ❓ 6: 黄金集 7 个新例具体数值 — 实施时定,先写 1-2 例占位
- ❓ 7: 求解器 fallback 是否真为非破坏(加 optional 参数)— 实施时验证
- ❓ 8: `get_document_repo` 是否需 lifespan 关闭 — 实施时定
- ❓ 9: 9 项之间是否真有依赖?实际可能 2/3 互不依赖 — 实施时可并行
- ❓ 10: RAG 端 ENGINE_URL 当前是否真在用 — 待确认

---

## §8 实施顺序(垂直切片,每项独立闭环)

按 C 方案,每项 = 独立 TDD 小循环 + commit + review。

1. **D1 + #10** (X-Admin-Key + .env):admin/security.py + test_admin_key + .env.example
2. **#1** (3 张表):DocumentRepo + 0006 migration + test_documents_repo + lifespan
3. **#2** (/trace):trace.py + test_trace,使用 #1 的 DocumentRepo
4. **#7 + #8** (audit 2 字段):audit.py schema + shared/ekrs_shared/audit.py 白名单
5. **#3 + #4** (/calculate + fallback):solver.solve 加 allow_soft_fallback + calculate.py + test_fallback + test_calculate
6. **#5** (黄金集 7 例):fixtures/golden/v2/ + 索引更新。**注:黄金集为静态数据,允许 1 commit 跨 500 LOC 上限(CQ2 决议)**
7. **#6** (覆盖率):末态跑 --cov,补真实缺测路径
8. **#9** (handbook 同步):合并到各 commit,无独立 commit
9. **打 tag**:`phase6a-spec-closure`

每步 commit + subagent review 闸门。
