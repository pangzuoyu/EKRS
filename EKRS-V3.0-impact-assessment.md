# EKRS V3.0 规范变更影响评估

**分支:** master
**日期:** 2026-04-22
**状态:** 评估完成，待迁移计划

---

## 一、Scope Check

- **Intent:** 评估 V3.0 规范变更对当前 Phase 2 solver core 实现的影响
- **Delivered:** 当前 master 分支 Phase 2 solver core 已实现（90 tests passing），V3.0 是新规范需要评估迁移路径
- **结论:** CLEAN — 这不是 PR review，是规范评估

---

## 二、IR V2 破坏性变更分析

V3.0 的 Constraint IR 从 V1 升级到 V2，是**结构性破坏性变更**，无法渐进迁移。

### 2.1 字段变更对照

| V1 字段 | V2 字段 | 变更类型 |
|---------|---------|---------|
| `operator: str` (<=, >=, ==, range) | `value_type: "interval"` + `interval.lower/upper/lower_inclusive/upper_inclusive` | 重构 |
| `value: Union[float, Tuple[float,float]]` | `interval?: {...}` 或标量直接存 | 重构 |
| `Priority: IntEnum` (NATIONAL=100, ...) | `priority: {explicit_level, recency_score, authority_score}` | 重构 |
| `source: dict` | `source: {doc_id, provision_id, doc_type, authority_score}` | 重构 |
| `conditions: List[Condition]` (flat) | `conditions: Condition[]` (相同名字但语义可能变) | 扩展 |
| 无 | `inferred: bool`, `confidence: number` | 新增 |
| `content_hash`, `version` (顶层) | 移入 `lifecycle: {version, effective_date, expiry_date}` | 重组 |

### 2.2 依赖链追踪（Big Bang 原因）

```
shared/ekrs_shared/models.py (Constraint V1 定义)
    ↓
rag/ekrs_rag/constraint_engine/parser.py → produces Constraint V1
    ↓
rag/ekrs_rag/constraint_engine/evidence_builder.py → produces Constraint V1
    ↓
rag/ekrs_rag/constraint_engine/solver.py → consumes Constraint V1
    ↓ (纯函数)
rag/ekrs_rag/api/routes/constraints.py → returns solver result
    ↓
rag/tests/unit/test_solver.py (20+ test files)
rag/tests/golden_set/test_golden_set.py
```

如果只改 `models.py`，solver 的 `_operator_to_interval()` 会崩溃。
如果只改 solver，builder 产生的对象 solver 不认识。
如果只改 builder，tests 会失败。

**结论:** 必须 `feature/v2-migration` 分支一次性重写全链路。

---

## 三、尚未解决的问题（Q&A）

### Q1: IR V2 迁移范围多大？能否渐进迁移？

**答案:** Big Bang，无法渐进。

所有文件必须同时更新：
1. `shared/ekrs_shared/models.py` — V2 模型（保留 V1 或直接替换）
2. `rag/ekrs_rag/constraint_engine/parser.py` — 适配 V2（parse_interval 输出 `interval` 而不是 `operator+value`）
3. `rag/ekrs_rag/constraint_engine/evidence_builder.py` — 适配 V2
4. `rag/ekrs_rag/constraint_engine/solver.py` — 适配 V2（增加 value_type 分派、strict mode）
5. `rag/ekrs_rag/api/routes/constraints.py` — 适配 V2 response
6. 全量测试重写（20+ files）

### Q2: MissingContextError 应该放在 `shared/` 还是 `rag/`？

**答案:** API 层抛出，`shared/` 不需要定义此异常。

- 铁律 R2: Solver 是纯函数，无 I/O，无状态
- 铁律 R6: strict mode 是 API 层的行为，不是 solver 内部的行为

正确流程：
```python
# constraints.py
if strict:
    for c in constraints:
        if c.inferred:
            raise HTTPException(400, "missing_context: inferred constraint not allowed in strict mode")
result = IntervalSolver.solve(constraints, ...)
```

`solver.py` 本身不抛异常，只返回 `{"status": "CONFLICT", ...}`，API 层映射为 200 + conflicts。

### Q3: TC_STRICT_01 需要 API 测试框架还是纯单元测试？

**答案:** 需要集成测试（FastAPI TestClient），纯单元测试无法覆盖。

- `strict=true` + `inferred=true` → `400 missing_context` 是 **API 层** 的行为
- `IntervalSolver.solve()` 本身是纯函数，不知道 strict mode 的存在
- 当前 `rag/tests/integration/` 只有 `test_ingestion.py`，缺少 `test_constraints.py`

最小工作量：
1. 新建 `rag/tests/integration/test_constraints_api.py`
2. 用 `pytest` + `fastapi.testclient` 或 `httpx.AsyncClient`
3. Mock retriever 注入

---

## 四、V3.0 新增测试用例（Section 9.2 黄金集）

| 用例 | 描述 | 输入 | 期望 |
|------|------|------|------|
| TC_DRAFT_01 | 征求意见稿状态识别 | `text: "温度不得超过80℃（征求意见稿）"` | `lifecycle.status = "draft"`, `is_binding = false` |
| TC_UNIT_01 | 开尔文转摄氏度 | `text: "温度 ≤ 300K"` | `interval.upper = 26.85`, `unit = "C"` |
| TC_REVIEW_01 | 审阅意见识别 | `text: "建议将温度上限改为70℃"` | `lifecycle.status = "review"`, `is_binding = false` |
| TC_OPEN_01 | 开区间识别 | `text: "温度 > 50℃"` | `lower = 50`, `lower_inclusive = false` |
| TC_TRANSITION_01 | 过渡期标准 | `doc_meta.status = "transitional"` | `lifecycle.status = "transitional"`, `is_binding = true` |
| TC_STRICT_01 | 严格模式拒绝 inferred | `inferred = true, strict = true` | 返回 `400 missing_context` |
| TC_HARD_CONFLICT_01 | 硬冲突检测 | `[0,50] 与 [60,100]` | 返回 `409 conflict` |

**注意:** TC_STRICT_01 和 TC_HARD_CONFLICT_01 需要 API 层测试，不是纯 solver 单元测试。

---

## 五、V3.0 新增函数需求

### 5.1 `infer_lifecycle()` — 生命周期推断规则（L5）

根据文本关键词和文档元数据推断约束的生命周期状态：

| 场景 | 触发条件 | lifecycle.status | is_binding |
|------|---------|-----------------|------------|
| 征求意见稿 | 文件名或文本含 draft / 征求意见稿 | draft | false |
| 审阅意见 | doc_type == "review" 或文本含 建议 / 审阅 | review | false |
| 过渡期标准 | 文本含 过渡期 或 transition period | transitional | true |
| 正式生效 | 默认 | active | true |
| 已被替代 | 文档被新版本替代 | deprecated | false |

### 5.2 `parse_interval()` 增强 — 开区间支持

当前实现只处理闭区间。V3.0 要求支持：
- `>` / `大于` / `高于` → `lower_inclusive = false`
- `<` / `小于` / `低于` → `upper_inclusive = false`

### 5.3 单位归一化规则（L3）扩展

当前 `normalize_temperature` 存在，需要检查：
- `°C`/`℃`/`Celsius` → `C`
- `K` → `C`（仿射变换：`K - 273.15`）
- `MPa`/`Pa` → 压强乘性变换
- `psi` → `Pa`（乘以 6894.76）

---

## 六、影响分类汇总

### 🔴 高影响 — 需要重写

1. **数据模型 V1→V2 重构** — `shared/ekrs_shared/models.py` 大规模重构
2. **Builder 层适配** — `parser.py` + `evidence_builder.py` 适配新 IR
3. **Solver 层适配** — 增加 `value_type` 分派，支持 `interval` 结构
4. **严格模式 API 层实现** — `constraints.py` 增加 `inferred` 检查 + 400 错误

### 🟡 中等影响 — 需要新增

5. **黄金集测试用例** — TC_DRAFT_01, TC_UNIT_01, TC_REVIEW_01, TC_OPEN_01, TC_TRANSITION_01, TC_STRICT_01, TC_HARD_CONFLICT_01
6. **生命周期推断函数** — `infer_lifecycle()` 实现 L5 规则表
7. **开区间解析增强** — `_operator_to_interval()` 支持 `lower_inclusive=false`
8. **集成测试框架** — `test_constraints_api.py` + FastAPI TestClient fixtures

### 🟢 低影响 — 已实现或无需修改

- R1 证据化 Hint — 已有
- R2 纯函数 Solver — 已有
- R4 显式优先级 — 已有
- R5 轻量 KG — 无图数据库依赖
- R7 作用域隔离 — `scope_path` 已有
- R8 索引层洁净 — 设计原则，无需代码改动

---

## 七、迁移建议

**创建 `feature/v2-migration` 工作分支**，在单独分支处理 IR V2 迁移，不要污染 master。

建议计划结构：
1. `models.py` 重写为 V2
2. `parser.py` 适配 V2 输出
3. `evidence_builder.py` 适配 V2
4. `solver.py` 适配 V2（value_type 分派、严格模式）
5. `constraints.py` API 层严格模式 + 400 错误
6. 新增 `infer_lifecycle()` + 开区间解析增强
7. 黄金集新增 7 个测试用例
8. 新建集成测试 `test_constraints_api.py`
9. 全量测试通过后 merge master

---

## 八、已保存记忆

以下发现已记录到长期记忆（memory 工具）：
- IR V2 是破坏性重构，Big Bang 迁移
- Strict mode 在 API 层，不在 solver 层
- TC_STRICT_01 需要集成测试
- V3.0 新增 7 个黄金集测试用例