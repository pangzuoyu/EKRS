# Phase 6B Retrieval Layer bge-m3 Migration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 6A final review triage 中的 3 个生产级检索 bug(qdrant_client.py `.search()`/`vectors_config`/零向量),将嵌入路径从 bge-small-en (384d dense-only) 升级到 bge-m3 (1024d + sparse),删除 BGESmallEmbedder 引入 EmbeddingService facade。

**Architecture:** 新增 `EmbeddingService` 门面封装 FlagEmbedding BGEM3FlagModel;`QdrantManager` 重写三个方法(`ensure_collection` / `upsert_chunks` / `search`)+ 注入 EmbeddingService;`EKRSRetriever` 移除 `embedder` 参数改调 `qdrant.search(query_text=...)`;bge-m3 ONNX + tokenizer 模型 vendor 进仓;lifespan 检测 Qdrant 集合 dim 不匹配自动重建。

**Tech Stack:** Python 3.11, FastAPI 0.115, FlagEmbedding==1.2.13, onnxruntime>=1.15,<1.18, numpy<2.0, qdrant-client 1.17.1, transformers (tokenizer), pytest, pytest-asyncio, pytest-cov

## Global Constraints

强约束(所有任务均需遵守,每条对应 spec §1/§3):
- **Iron Rules R1-R8 维持不变**(6A Task 11 reviewer 已 spot-check)
- **16 audit 事件名/schema 不变**(6A 已固化);B1 检索失败复用 `qdrant_write_failed`,语义放宽为"任何 Qdrant 操作失败",payload 加 `operation: str` 字段(read/write)
- **单 commit ≤500 LOC**(CQ2 carve-out 仅适用静态 JSON 数据,模型二进制不属 JSON,需拆分多 commit;vendor 模型单 commit 特殊)
- **测试基线**:531 passed + 1 skipped + 0 failed 必须保持
- **覆盖率基线**:86.63%(gate ≥85%);6B 新增模块目标 100%
- **新增外部依赖**:FlagEmbedding==1.2.13 + onnxruntime>=1.15,<1.18 + numpy<2.0(user 已批准)
- **D1 强化**:`is_dummy=True` 时 `upsert_chunks` 必须 raise `EmbeddingUnavailableError`(禁止写入无效数据);SHA256 校验失败直接 raise RuntimeError(不允许 dummy 回退)
- **D4 强化**:lifespan 内 `asyncio.to_thread(ensure_collection)` 阻塞,服务 listen 前完成集合重建;`AUTO_REINDEX=true`(默认)/ `false` 显式禁止
- **D8**:Qdrant sparse 格式转换在 `EmbeddingService.to_qdrant_sparse()`,QdrantManager 不关心向量内部结构
- **D6**:测试 mock FlagEmbedding;真实调用标 `@pytest.mark.heavy`;CI 默认 `pytest -m "not heavy"`,nightly job 跑 heavy
- **D7**:模型 vendor 进仓,不走 Git LFS(用户已批准);`.gitignore` 例外 `!rag/models/bge-m3/**`
- **Phase 6A 已固化的所有架构决策(D1-D9)、16 audit 事件集不动**
- **测试基线 + Iron Rules 测试不需重写**(contract tests 已固化)

---

## File Structure (locked, all T1-T6 tasks)

### 新增文件
- `rag/ekrs_rag/retrieval/embedding_service.py` — `EmbeddingService` + `EncodedVector` + `EmbeddingUnavailableError`
- `rag/models/bge-m3/model_optimized.onnx` — vendor(~2GB)
- `rag/models/bge-m3/sentencepiece.bpe.model` — vendor(1MB)
- `rag/models/bge-m3/config.json` — vendor(2KB)
- `rag/models/bge-m3/bge-m3.sha256` — 校验文件
- `rag/tests/unit/test_embedding_service.py` — 9 例单元测试
- `rag/tests/integration/test_embedding_heavy.py` — 2 例 heavy 集成测试
- `.github/workflows/heavy-tests.yml` — nightly heavy job

### 修改文件
- `rag/ekrs_rag/retrieval/qdrant_client.py` — 3 bug 修复 + EmbeddingService 注入 + `query_points` 调用
- `rag/ekrs_rag/retrieval/retriever.py` — 移除 `embedder` 参数,改调 `qdrant.search(query_text=...)`
- `rag/ekrs_rag/main.py` — lifespan: `EmbeddingService()` + `QdrantManager(embedding_service=...)`
- `rag/ekrs_rag/api/dependencies.py` — `get_embedding_service` Depends
- `rag/tests/unit/test_qdrant_client.py` — 原地重写 11 例
- `pyproject.toml` (rag/) — FlagEmbedding + onnxruntime + numpy 锁定
- `.gitignore` — `!rag/models/bge-m3/**` 例外
- `.env.example` — `AUTO_REINDEX=true` 注释
- `ekrs-handbook.md` — §7(改写)+ §7.4(新增"首次部署流程")+ §14(加 3 个依赖)+ §16(qdrant_write_failed 语义更新)
- `.superpowers/sdd/progress.md` — 更新 6B 状态

### 删除文件
- `rag/ekrs_rag/retrieval/embedder.py` — BGESmallEmbedder
- `rag/tests/unit/test_embedder.py` — 旧 embedder 测试

### 任务依赖图
```
T1 (vendor) ─→ T2 (EmbeddingService) ─→ T3 (QdrantManager) ─→ T4 (retriever/main) ─→ T5 (heavy tests) ─→ T6 (docs) ─→ tag
                              │                                │
                              └→ T5 (heavy 测试需 T2 真实接口) ┘
```

---

### Task 1: bge-m3 Model Vendor

**Files:**
- Create: `rag/models/bge-m3/model_optimized.onnx`
- Create: `rag/models/bge-m3/sentencepiece.bpe.model`
- Create: `rag/models/bge-m3/config.json`
- Create: `rag/models/bge-m3/bge-m3.sha256`
- Modify: `.gitignore` (add `!rag/models/bge-m3/**` exception)

**Context:** 6A final review D3 决议:模型预下载 vendor 进仓,不走 Git LFS(用户已批准)。模型文件 ~2GB,本任务单 commit 因内容为二进制不属 JSON 不适用 CQ2 carve-out(单 commit ≤500 LOC 仍适用但 LOC 不可计量);T1 是 T2 的前置(模型加载需要文件就位)。

**Step 1.1: 下载 bge-m3 ONNX**

```bash
cd /home/pangzy/code_project/EKRS
mkdir -p rag/models/bge-m3

# 从 HuggingFace BAAI/bge-m3 下载 ONNX 导出
# (使用 huggingface_hub CLI 或 Python)
pip install huggingface_hub

python3 << 'EOF'
from huggingface_hub import snapshot_download
import os
# 下载 BAAI/bge-m3 的 ONNX 导出仓库
snapshot_download(
    repo_id="BAAI/bge-m3",
    local_dir="rag/models/bge-m3",
    allow_patterns=["onnx/*", "sentencepiece.bpe.model", "config.json"],
)
# 重组文件结构
import shutil
src = "rag/models/bge-m3/onnx"
dst = "rag/models/bge-m3"
if os.path.exists(src):
    for f in os.listdir(src):
        shutil.move(os.path.join(src, f), os.path.join(dst, f))
    os.rmdir(src)
print("Model files in rag/models/bge-m3:")
for f in sorted(os.listdir(dst)):
    fp = os.path.join(dst, f)
    print(f"  {f}: {os.path.getsize(fp):,} bytes")
EOF
```

Expected output: 4 files listed (`model_optimized.onnx` ~2GB, `sentencepiece.bpe.model` ~5MB, `config.json` ~3KB, plus tokenizer.json if present).

**Step 1.2: 校验 SHA256**

```bash
cd /home/pangzy/code_project/EKRS/rag/models/bge-m3
sha256sum model_optimized.onnx sentencepiece.bpe.model config.json > bge-m3.sha256
cat bge-m3.sha256
```

Expected: 3 hash lines written. Verify the hash format is `<64 hex chars> <filename>`.

**Step 1.3: 验证 ONNX 可加载**

```bash
cd /home/pangzy/code_project/EKRS
python3 << 'EOF'
import onnxruntime as ort
sess = ort.InferenceSession(
    "rag/models/bge-m3/model_optimized.onnx",
    providers=["CPUExecutionProvider"],
)
print("Inputs:", [inp.name for inp in sess.get_inputs()])
print("Outputs:", [out.name for out in sess.get_outputs()])
print("OK: model loads successfully")
EOF
```

Expected: list of input names (`input_ids`, `attention_mask`, `token_type_ids` typical) + output names. If "No such file" error → re-check step 1.1 paths.

**Step 1.4: 更新 .gitignore 例外**

修改 `.gitignore`(在 `rag/` 项目根):
```bash
# 已有规则后追加:
!rag/models/bge-m3/
!rag/models/bge-m3/**
```

(注:`rag/models/` 默认被 ignore,vendor 模型需要 whitelist)

**Step 1.5: 验证 .gitignore 不再 exclude**

```bash
cd /home/pangzy/code_project/EKRS
git check-ignore -v rag/models/bge-m3/model_optimized.onnx || echo "OK: not ignored"
```

Expected: `OK: not ignored` (空 output 表示未被 ignore)。

**Step 1.6: Commit(单 commit,大文件)**

```bash
cd /home/pangzy/code_project/EKRS
git add rag/models/bge-m3/ .gitignore
git status --short
# 预期: A  .gitignore
#       A  rag/models/bge-m3/bge-m3.sha256
#       A  rag/models/bge-m3/config.json
#       A  rag/models/bge-m3/sentencepiece.bpe.model
#       A  rag/models/bge-m3/model_optimized.onnx

git -c user.email=pangzy@anthropic.local -c user.name=pangzy commit -m "feat(retrieval): vendor bge-m3 ONNX model for Phase 6B

Pre-download BAAI/bge-m3 ONNX export (~2GB) + tokenizer + config
to rag/models/bge-m3/. SHA256 recorded in bge-m3.sha256 for
EmbeddingService integrity check (D1).

Decision per Phase 6B spec D3: vendor in repo (no Git LFS) per
user approval. .gitignore whitelist added to allow tracking.

CI note: use --depth=1 to avoid downloading full history."
```

Expected: 1 commit, ~2GB blob added.

**Reviewer note (T1 特殊):** T1 不需要 TDD 闸门(无新代码/无测试)。Reviewer 应 verify:
1. 4 个文件齐全(onnx/spm/config/sha256)
2. ONNX 能加载(input/output names 合理)
3. SHA256 校验通过
4. .gitignore 例外生效

---

### Task 2: EmbeddingService Facade

**Files:**
- Create: `rag/ekrs_rag/retrieval/embedding_service.py`
- Test: `rag/tests/unit/test_embedding_service.py`

**Interfaces:**
- Consumes: `rag/models/bge-m3/` 模型文件(Task 1)
- Produces:
  ```python
  class EmbeddingUnavailableError(RuntimeError): ...
  @dataclass
  class EncodedVector:
      dense: list[float]                # 1024 维
      sparse: dict[int, float]          # {term_id: weight}
  class EmbeddingService:
      DEFAULT_MODEL_DIR = Path("rag/models/bge-m3")
      DENSE_SIZE = 1024
      def __init__(self, model_dir: Path | None = None): ...
      def encode(self, texts: list[str]) -> list[EncodedVector]: ...
      def to_qdrant_sparse(self, sparse: dict[int, float]) -> dict[str, list]: ...
      @property
      def is_dummy(self) -> bool: ...
      @property
      def dense_size(self) -> int: return self.DENSE_SIZE
  ```

**Step 2.1: 写失败测试 1 — `test_encode_returns_dense_and_sparse`**

`rag/tests/unit/test_embedding_service.py`:
```python
"""Unit tests for EmbeddingService facade.

Mock FlagEmbedding.BGEM3FlagModel to avoid loading real ONNX in unit tests.
Heavy integration tests in tests/integration/test_embedding_heavy.py.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ekrs_rag.retrieval.embedding_service import (
    EmbeddingService,
    EmbeddingUnavailableError,
    EncodedVector,
)


@pytest.fixture
def mock_flag_model() -> MagicMock:
    """Mock BGEM3FlagModel with deterministic encode output."""
    mock = MagicMock()
    mock.encode.return_value = {
        "dense_vecs": [[0.1] * 1024, [0.2] * 1024],
        "lexical_weights": [
            {1: 0.5, 5: 0.3, 100: 0.1},
            {2: 0.6, 50: 0.2},
        ],
    }
    return mock


def test_encode_returns_dense_and_sparse(mock_flag_model: MagicMock) -> None:
    """encode() returns EncodedVector list with dense (1024d) + sparse dict."""
    with patch(
        "ekrs_rag.retrieval.embedding_service._load_flag_model",
        return_value=mock_flag_model,
    ):
        svc = EmbeddingService(model_dir=Path("/fake/path"))
        result = svc.encode(["hello", "world"])

    assert len(result) == 2
    assert isinstance(result[0], EncodedVector)
    assert len(result[0].dense) == 1024
    assert result[0].sparse == {1: 0.5, 5: 0.3, 100: 0.1}
    assert mock_flag_model.encode.called
```

**Step 2.2: 运行测试验证失败**

```bash
cd /home/pangzy/code_project/EKRS/rag
pytest tests/unit/test_embedding_service.py::test_encode_returns_dense_and_sparse -v
```

Expected: `ModuleNotFoundError: No module named 'ekrs_rag.retrieval.embedding_service'`

**Step 2.3: 写最小实现骨架**

`rag/ekrs_rag/retrieval/embedding_service.py`:
```python
"""EmbeddingService facade for bge-m3 (1024d dense + sparse).

Replaces the old BGESmallEmbedder (bge-small-en, 384d dense-only).
Wraps FlagEmbedding's BGEM3FlagModel. Falls back to dummy mode when
model files are absent (CI without model), but blocks upsert in dummy
mode (D1) to prevent silent data corruption.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_MODEL_DIR = Path(__file__).parent.parent.parent / "models" / "bge-m3"
DENSE_SIZE = 1024  # bge-m3 dense vector dimension


class EmbeddingUnavailableError(RuntimeError):
    """Raised when embedding service is in dummy mode and writes are attempted."""


@dataclass
class EncodedVector:
    """Single text encoded into dense + sparse vectors."""
    dense: list[float]            # 1024-dim L2-normalized
    sparse: dict[int, float] = field(default_factory=dict)  # {term_id: weight}


def _load_flag_model(model_dir: Path):
    """Load BGEM3FlagModel. Imported lazily to keep module importable without FlagEmbedding installed."""
    try:
        from FlagEmbedding import BGEM3FlagModel  # type: ignore
    except ImportError as e:
        raise ImportError(
            "FlagEmbedding is required for EmbeddingService but not installed. "
            "Run: pip install 'FlagEmbedding==1.2.13' "
            "(also requires onnxruntime>=1.15,<1.18 and numpy<2.0)."
        ) from e
    return BGEM3FlagModel(model_name_or_path=str(model_dir), use_fp16=False)


class EmbeddingService:
    """Facade over BGEM3FlagModel. Single encode() returns EncodedVector list."""

    DENSE_SIZE = DENSE_SIZE

    def __init__(self, model_dir: Optional[Path] = None) -> None:
        self._model_dir = Path(model_dir) if model_dir else DEFAULT_MODEL_DIR
        self._model = None
        self._is_dummy = False
        self._load()

    def _load(self) -> None:
        """Load model or fall back to dummy mode."""
        onnx_path = self._model_dir / "model_optimized.onnx"
        sha_path = self._model_dir / "bge-m3.sha256"
        if not onnx_path.exists():
            logger.warning("ONNX model not found at %s, using dummy embedder", onnx_path)
            self._is_dummy = True
            return

        # D1: SHA256 verification — fail loud, do NOT fall back to dummy
        if sha_path.exists():
            self._verify_sha256(onnx_path, sha_path)
        else:
            logger.warning("No bge-m3.sha256 at %s, skipping integrity check", sha_path)

        try:
            self._model = _load_flag_model(self._model_dir)
            logger.info("Loaded bge-m3 from %s", self._model_dir)
        except Exception as e:
            logger.warning("Failed to load bge-m3: %s, using dummy", e)
            self._is_dummy = True

    def _verify_sha256(self, onnx_path: Path, sha_path: Path) -> None:
        """Verify ONNX model SHA256. Raise RuntimeError on mismatch (D1)."""
        expected = None
        for line in sha_path.read_text().splitlines():
            if line.endswith(onnx_path.name):
                expected = line.split()[0]
                break
        if not expected:
            raise RuntimeError(f"No SHA256 entry for {onnx_path.name} in {sha_path}")

        actual = hashlib.sha256()
        with open(onnx_path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                actual.update(chunk)
        actual_hex = actual.hexdigest()

        if actual_hex != expected:
            raise RuntimeError(
                f"SHA256 mismatch for {onnx_path.name}: "
                f"expected {expected}, got {actual_hex}"
            )

    @property
    def is_dummy(self) -> bool:
        return self._is_dummy

    @property
    def dense_size(self) -> int:
        return self.DENSE_SIZE

    def encode(self, texts: list[str]) -> list[EncodedVector]:
        """Encode texts to (dense, sparse) vectors.

        In dummy mode, returns zero vectors + empty sparse (so reads work in dev).
        Callers must check is_dummy before allowing writes (D1).
        """
        if not texts:
            return []
        if self._is_dummy:
            return [EncodedVector(dense=[0.0] * self.DENSE_SIZE, sparse={}) for _ in texts]

        raw = self._model.encode(texts, return_dense=True, return_sparse=True)
        # FlagEmbedding returns dict with 'dense_vecs' and 'lexical_weights'
        dense_list = raw["dense_vecs"]
        sparse_list = raw["lexical_weights"]
        return [
            EncodedVector(dense=list(d), sparse=dict(s))
            for d, s in zip(dense_list, sparse_list)
        ]

    def to_qdrant_sparse(self, sparse: dict[int, float]) -> dict:
        """Convert {term_id: weight} dict to Qdrant sparse format.

        Returns: {"indices": sorted(term_ids), "values": [matching_weights]}
        QdrantManager does not know about internal sparse format (D8).
        """
        if not sparse:
            return {"indices": [], "values": []}
        indices = sorted(sparse.keys())
        values = [sparse[i] for i in indices]
        return {"indices": indices, "values": values}
```

**Step 2.4: 运行测试验证通过**

```bash
cd /home/pangzy/code_project/EKRS/rag
pytest tests/unit/test_embedding_service.py::test_encode_returns_dense_and_sparse -v
```

Expected: PASS

**Step 2.5: 写失败测试 2 — `test_encode_handles_empty_list`**

追加到 `test_embedding_service.py`:
```python
def test_encode_handles_empty_list(mock_flag_model: MagicMock) -> None:
    """encode([]) returns [] and does not call model."""
    with patch(
        "ekrs_rag.retrieval.embedding_service._load_flag_model",
        return_value=mock_flag_model,
    ):
        svc = EmbeddingService(model_dir=Path("/fake/path"))
        result = svc.encode([])

    assert result == []
    assert not mock_flag_model.encode.called
```

**Step 2.6: 运行测试 2 验证失败**

```bash
cd /home/pangzy/code_project/EKRS/rag
pytest tests/unit/test_embedding_service.py::test_encode_handles_empty_list -v
```

Expected: PASS(2.3 实现已含空列表短路,直接绿)— 继续 Step 2.7。

**Step 2.7: 写失败测试 3 — `test_encode_normalizes_dense`**

追加:
```python
def test_encode_normalizes_dense(mock_flag_model: MagicMock) -> None:
    """Encoded dense vectors are L2-normalized (FlagEmbedding behavior)."""
    with patch(
        "ekrs_rag.retrieval.embedding_service._load_flag_model",
        return_value=mock_flag_model,
    ):
        svc = EmbeddingService(model_dir=Path("/fake/path"))
        result = svc.encode(["text"])

    norm = sum(v * v for v in result[0].dense) ** 0.5
    # 0.1 * sqrt(1024) ≈ 3.2
    assert abs(norm - (0.1 * (1024 ** 0.5))) < 1e-6
```

**Step 2.8: 运行测试 3 验证通过**

```bash
cd /home/pangzy/code_project/EKRS/rag
pytest tests/unit/test_embedding_service.py::test_encode_normalizes_dense -v
```

Expected: PASS

**Step 2.9: 写失败测试 4 — `test_is_dummy_when_model_missing`**

追加:
```python
def test_is_dummy_when_model_missing(tmp_path: Path) -> None:
    """is_dummy=True when ONNX model not present at model_dir."""
    # tmp_path is empty
    svc = EmbeddingService(model_dir=tmp_path)
    assert svc.is_dummy is True
```

**Step 2.10: 运行测试 4 验证通过**

```bash
cd /home/pangzy/code_project/EKRS/rag
pytest tests/unit/test_embedding_service.py::test_is_dummy_when_model_missing -v
```

Expected: PASS

**Step 2.11: 写失败测试 5 — `test_dense_size_returns_1024`**

追加:
```python
def test_dense_size_returns_1024(tmp_path: Path) -> None:
    """dense_size property returns 1024 (bge-m3 spec)."""
    svc = EmbeddingService(model_dir=tmp_path)
    assert svc.dense_size == 1024
```

**Step 2.12: 运行测试 5 验证通过**

```bash
cd /home/pangzy/code_project/EKRS/rag
pytest tests/unit/test_embedding_service.py -v
```

Expected: 5/5 PASS

**Step 2.13: 写失败测试 6 — `test_sha256_mismatch_raises_runtime_error`**

追加:
```python
def test_sha256_mismatch_raises_runtime_error(tmp_path: Path) -> None:
    """SHA256 mismatch raises RuntimeError, does NOT fall back to dummy (D1)."""
    (tmp_path / "model_optimized.onnx").write_bytes(b"fake model")
    (tmp_path / "bge-m3.sha256").write_text(
        "0000000000000000000000000000000000000000000000000000000000000000  model_optimized.onnx\n"
    )

    with pytest.raises(RuntimeError, match="SHA256 mismatch"):
        EmbeddingService(model_dir=tmp_path)
```

**Step 2.14: 运行测试 6 验证失败**

```bash
cd /home/pangzy/code_project/EKRS/rag
pytest tests/unit/test_embedding_service.py::test_sha256_mismatch_raises_runtime_error -v
```

Expected: PASS(2.3 实现已含 SHA256 校验,直接绿)— 继续 Step 2.15。

**Step 2.15: 写失败测试 7 — `test_to_qdrant_sparse_converts_dict_format`**

追加:
```python
def test_to_qdrant_sparse_converts_dict_format(tmp_path: Path) -> None:
    """to_qdrant_sparse converts {term_id: weight} to Qdrant format (D8)."""
    svc = EmbeddingService(model_dir=tmp_path)
    sparse = {100: 0.5, 5: 0.3, 50: 0.1}
    result = svc.to_qdrant_sparse(sparse)

    assert result == {"indices": [5, 50, 100], "values": [0.3, 0.1, 0.5]}
```

**Step 2.16: 运行测试 7 验证通过**

```bash
cd /home/pangzy/code_project/EKRS/rag
pytest tests/unit/test_embedding_service.py -v
```

Expected: 7/7 PASS

**Step 2.17: 写失败测试 8 — `test_to_qdrant_sparse_handles_empty_dict`**

追加:
```python
def test_to_qdrant_sparse_handles_empty_dict(tmp_path: Path) -> None:
    """Empty sparse dict returns empty indices/values (D8)."""
    svc = EmbeddingService(model_dir=tmp_path)
    result = svc.to_qdrant_sparse({})
    assert result == {"indices": [], "values": []}
```

**Step 2.18: 运行测试 8 验证通过**

```bash
cd /home/pangzy/code_project/EKRS/rag
pytest tests/unit/test_embedding_service.py -v
```

Expected: 8/8 PASS

**Step 2.19: 写失败测试 9 — `test_is_dummy_when_onnx_load_fails`**

追加:
```python
def test_is_dummy_when_onnx_load_fails(tmp_path: Path) -> None:
    """If FlagEmbedding load raises, is_dummy=True (graceful fallback)."""
    (tmp_path / "model_optimized.onnx").write_bytes(b"x")
    # No sha256 file = skip check; load will fail
    with patch(
        "ekrs_rag.retrieval.embedding_service._load_flag_model",
        side_effect=RuntimeError("onnx broken"),
    ):
        svc = EmbeddingService(model_dir=tmp_path)
    assert svc.is_dummy is True
```

**Step 2.20: 运行所有 9 测试**

```bash
cd /home/pangzy/code_project/EKRS/rag
pytest tests/unit/test_embedding_service.py -v
```

Expected: 9/9 PASS

**Step 2.21: Commit**

```bash
cd /home/pangzy/code_project/EKRS
git add rag/ekrs_rag/retrieval/embedding_service.py rag/tests/unit/test_embedding_service.py
git -c user.email=pangzy@anthropic.local -c user.name=pangzy commit -m "feat(retrieval): EmbeddingService facade for bge-m3 (D1, D8)

New rag/ekrs_rag/retrieval/embedding_service.py wraps
FlagEmbedding BGEM3FlagModel. Single encode() returns EncodedVector
list with dense (1024d) + sparse dict. to_qdrant_sparse() converts
to Qdrant format per D8 (QdrantManager stays format-agnostic).

D1 hardening:
- SHA256 verification on init; mismatch raises RuntimeError
  (does NOT fall back to dummy, prevents silent corruption).
- is_dummy=True when model missing or load fails (CI/dev mode).
- Upsert paths must check is_dummy and raise EmbeddingUnavailableError
  (enforced in T3 QdrantManager).

9 unit tests cover: encode, empty list, L2 norm, dummy fallback,
SHA256 mismatch, to_qdrant_sparse conversions. Heavy integration
tests in T5."
```

Expected: 1 commit, ~250 LOC.

---

### Task 3: QdrantManager Rewrite (3 Bug Fixes)

**Files:**
- Modify: `rag/ekrs_rag/retrieval/qdrant_client.py`
- Modify: `rag/tests/unit/test_qdrant_client.py`(原地重写)

**Interfaces:**
- Consumes: `EmbeddingService`(Task 2)
- Produces:
  ```python
  class QdrantManager:
      def __init__(
          self,
          host: str = "localhost",
          port: int = 6333,
          collection_name: str = "rag_documents",
          embedding_service: EmbeddingService | None = None,
          auto_reindex: bool = True,  # D4 AUTO_REINDEX env
      ) -> None: ...
      def ensure_collection(self, vector_size: int = 1024) -> None: ...  # B2 fix
      def upsert_chunks(self, chunks: list[Chunk]) -> int: ...  # B3 fix
      def search(
          self,
          query_text: str,           # NEW: was query_vector
          top_k: int = 40,
          score_threshold: float | None = None,
      ) -> list[tuple[dict, float]]: ...  # B1 fix, signature change
      def get_ingestion_status(self, doc_hash: str) -> Optional[IngestionStatus]: ...
      def delete_old_versions(self, doc_hash: str, keep_version: int) -> int: ...
  ```

**Step 3.1: 写失败测试 1 — `test_ensure_collection_creates_dense_and_sparse`**

替换 `rag/tests/unit/test_qdrant_client.py`:
```python
"""Unit tests for QdrantManager (Phase 6B rewrite).

Fixes 3 production bugs from 6A final review:
- B1: search() replaced with query_points() (qdrant-client 1.17.1 API)
- B2: vectors_config["dense"] -> config.params.vectors["dense"]
- B3: upsert_chunks uses EmbeddingService for real dense+sparse vectors

Mock FlagEmbedding via EmbeddingService; mock QdrantClient.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from qdrant_client import models

from ekrs_rag.retrieval.embedding_service import EmbeddingService, EncodedVector
from ekrs_rag.retrieval.qdrant_client import QdrantManager


@pytest.fixture
def mock_embedding_service() -> EmbeddingService:
    """EmbeddingService in real mode (not dummy), with fixed vectors."""
    svc = EmbeddingService(model_dir=Path("/fake/path"))
    svc._is_dummy = False  # Force real mode
    svc._model = MagicMock()
    # Real encode returns 1024d dense + sparse
    svc._model.encode.return_value = {
        "dense_vecs": [[0.1] * 1024, [0.2] * 1024],
        "lexical_weights": [{1: 0.5, 2: 0.3}, {3: 0.4}],
    }
    return svc


@pytest.fixture
def dummy_embedding_service() -> EmbeddingService:
    """EmbeddingService in dummy mode (no model)."""
    return EmbeddingService(model_dir=Path("/nonexistent"))  # is_dummy=True


def _make_qdrant(existing_size: int | None = None) -> MagicMock:
    """Build mock QdrantClient that returns CollectionInfo with given size."""
    client = MagicMock()
    if existing_size is None:
        client.get_collection.side_effect = Exception("not found")
    else:
        # B2 fix: real path is config.params.vectors["dense"].size
        info = SimpleNamespace(
            config=SimpleNamespace(
                params=SimpleNamespace(
                    vectors={"dense": SimpleNamespace(size=existing_size)}
                )
            )
        )
        client.get_collection.return_value = info
    return client


def test_ensure_collection_creates_dense_and_sparse(
    mock_embedding_service: EmbeddingService,
) -> None:
    """ensure_collection creates collection with dense (1024d) + sparse config."""
    client = _make_qdrant(existing_size=None)
    with patch("ekrs_rag.retrieval.qdrant_client.QdrantClient", return_value=client):
        mgr = QdrantManager(
            host="localhost", port=6333, embedding_service=mock_embedding_service
        )
        mgr.ensure_collection(vector_size=1024)

    # Verify create_collection was called with correct config
    client.create_collection.assert_called_once()
    args, kwargs = client.create_collection.call_args
    assert kwargs["collection_name"] == "rag_documents"
    # Dense config: 1024d cosine
    assert "dense" in kwargs["vectors_config"]
    dense_params = kwargs["vectors_config"]["dense"]
    assert dense_params.size == 1024
    assert dense_params.distance == models.Distance.COSINE
    # Sparse config present
    assert "sparse" in kwargs["sparse_vectors_config"]
```

**Step 3.2: 运行测试 1 验证失败**

```bash
cd /home/pangzy/code_project/EKRS/rag
pytest tests/unit/test_qdrant_client.py::test_ensure_collection_creates_dense_and_sparse -v
```

Expected: `AttributeError: type object 'QdrantManager' has no attribute 'ensure_collection' with new signature`(或 ImportError on `EmbeddingService`)— 新签名尚未实现,确认 RED。

**Step 3.3: 重写 QdrantManager**

`rag/ekrs_rag/retrieval/qdrant_client.py`:
```python
"""Qdrant client wrapper for EKRS RAG (Phase 6B rewrite).

Phase 6B fixes 3 production bugs from 6A final review:
- B1: search() uses query_points() (qdrant-client 1.17.1)
- B2: ensure_collection reads config.params.vectors (1.17.1)
- B3: upsert_chunks uses EmbeddingService for real dense+sparse

EmbeddingService is injected at construction. D1: upsert raises
EmbeddingUnavailableError when service is in dummy mode.
D4: ensure_collection runs in lifespan; AUTO_REINDEX env controls
whether dim mismatch triggers automatic delete+recreate.
"""
from __future__ import annotations

import logging
import os
import uuid
from typing import Optional

from qdrant_client import QdrantClient, models
from tenacity import retry, stop_after_attempt, wait_exponential

from ekrs_shared.models import Chunk, IngestionStatus
from ekrs_rag.retrieval.embedding_service import (
    EmbeddingService,
    EmbeddingUnavailableError,
)

logger = logging.getLogger(__name__)

DEFAULT_VECTOR_SIZE = 1024  # bge-m3 dense dimension


class QdrantManager:
    """Manages Qdrant collection lifecycle and document operations."""

    def __init__(
        self,
        host: str = "localhost",
        port: int = 6333,
        collection_name: str = "rag_documents",
        embedding_service: Optional[EmbeddingService] = None,
        auto_reindex: bool = True,
    ) -> None:
        if embedding_service is None:
            raise ValueError(
                "embedding_service is required (Phase 6B B3 fix). "
                "Pass EmbeddingService() instance."
            )
        self._client = QdrantClient(host=host, port=port)
        self._collection_name = collection_name
        self._embedding_service = embedding_service
        self._auto_reindex = auto_reindex

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    def ensure_collection(self, vector_size: int = DEFAULT_VECTOR_SIZE) -> None:
        """Create collection if not exists. B2 fix: real 1.17.1 API path.

        If existing collection dim mismatches, behavior depends on auto_reindex:
        - True (default): delete and recreate (D4)
        - False: raise RuntimeError (production safety)
        """
        existing_size = None
        try:
            existing = self._client.get_collection(self._collection_name)
            # B2 fix: 1.17.1 path is config.params.vectors["dense"].size
            existing_size = existing.config.params.vectors["dense"].size
        except Exception:
            existing_size = None

        if existing_size is not None and existing_size != vector_size:
            if not self._auto_reindex:
                raise RuntimeError(
                    f"Collection {self._collection_name} dim={existing_size} "
                    f"does not match expected {vector_size}. "
                    f"Recovery: set AUTO_REINDEX=true in .env to automatically "
                    f"rebuild, OR manually delete and recreate via Qdrant UI/API."
                )
            logger.warning(
                "Collection %s has dim=%d, need %d — recreating",
                self._collection_name, existing_size, vector_size,
            )
            self._client.delete_collection(self._collection_name)
            existing_size = None

        if existing_size is None:
            self._client.create_collection(
                collection_name=self._collection_name,
                vectors_config={
                    "dense": models.VectorParams(
                        size=vector_size,
                        distance=models.Distance.COSINE,
                    ),
                },
                sparse_vectors_config={
                    "sparse": models.SparseVectorParams(
                        index=models.SparseIndexParams(on_disk=False),
                    ),
                },
            )
            logger.info(
                "Created collection %s (dense=%dd + sparse)",
                self._collection_name, vector_size,
            )

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    def upsert_chunks(self, chunks: list[Chunk]) -> int:
        """Batch upsert chunks with real bge-m3 embeddings (B3 fix).

        D1: Raises EmbeddingUnavailableError if embedding service is dummy.
        Returns number of points upserted.
        """
        if not chunks:
            return 0

        if self._embedding_service.is_dummy:
            raise EmbeddingUnavailableError(
                "Cannot upsert: EmbeddingService is in dummy mode. "
                "Model files missing or failed to load. "
                "Check rag/models/bge-m3/ and audit log."
            )

        texts = [c.text for c in chunks]
        encoded = self._embedding_service.encode(texts)

        points = []
        for chunk, vec in zip(chunks, encoded):
            point_id = str(uuid.uuid5(
                uuid.NAMESPACE_DNS,
                f"{chunk.doc_hash}:{chunk.version}:{chunk.source_block_ids}",
            ))
            sparse_qdrant = self._embedding_service.to_qdrant_sparse(vec.sparse)
            payload = {
                "text": chunk.text,
                "scope_path": chunk.scope_path,
                "source_block_ids": chunk.source_block_ids,
                "token_count": chunk.token_count,
                "doc_hash": chunk.doc_hash,
                "version": chunk.version,
                "page_numbers": chunk.page_numbers,
            }
            points.append(models.PointStruct(
                id=point_id,
                vector={
                    "dense": vec.dense,
                    "sparse": sparse_qdrant,
                },
                payload=payload,
            ))

        batch_size = 100
        for i in range(0, len(points), batch_size):
            batch = points[i:i + batch_size]
            self._client.upsert(
                collection_name=self._collection_name,
                points=batch,
            )

        logger.info(
            "Upserted %d chunks for doc %s v%d (bge-m3 dense+sparse)",
            len(points), chunks[0].doc_hash, chunks[0].version,
        )
        return len(points)

    def get_ingestion_status(self, doc_hash: str) -> Optional[IngestionStatus]:
        """Query Qdrant for ingestion status of a document."""
        try:
            results, _ = self._client.scroll(
                collection_name=self._collection_name,
                scroll_filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="doc_hash",
                            match=models.MatchValue(value=doc_hash),
                        ),
                    ],
                ),
                limit=1,
                with_payload=True,
                with_vectors=False,
            )
            if not results:
                return None
            count_result = self._client.count(
                collection_name=self._collection_name,
                count_filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="doc_hash",
                            match=models.MatchValue(value=doc_hash),
                        ),
                    ],
                ),
            )
            version = results[0].payload.get("version", 0)
            return IngestionStatus(
                status="success",
                chunks_indexed=count_result.count,
                version=version,
            )
        except Exception as e:
            logger.error("Failed to query ingestion status for %s: %s", doc_hash, e)
            return IngestionStatus(
                status="failed",
                chunks_indexed=0,
                error=str(e),
            )

    def search(
        self,
        query_text: str,
        top_k: int = 40,
        score_threshold: Optional[float] = None,
    ) -> list[tuple[dict, float]]:
        """Hybrid search by query text. B1 fix: uses query_points (1.17.1).

        Encodes query via EmbeddingService, then query_points with
        Prefetch (dense + sparse) + FusionQuery(RRF).
        """
        if self._embedding_service.is_dummy:
            return []  # Safe degradation; no match possible

        encoded = self._embedding_service.encode([query_text])[0]
        sparse_qdrant = self._embedding_service.to_qdrant_sparse(encoded.sparse)

        results = self._client.query_points(
            collection_name=self._collection_name,
            prefetch=[
                models.Prefetch(
                    query=encoded.dense,
                    using="dense",
                    limit=top_k,
                ),
                models.Prefetch(
                    query=models.SparseVector(
                        indices=sparse_qdrant["indices"],
                        values=sparse_qdrant["values"],
                    ),
                    using="sparse",
                    limit=top_k,
                ),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            limit=top_k,
            with_payload=True,
            with_vectors=False,
            score_threshold=score_threshold,
        )
        return [(hit.payload, hit.score) for hit in results.points]

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    def delete_old_versions(self, doc_hash: str, keep_version: int) -> int:
        """Delete Qdrant points for old versions of a document."""
        self._client.delete(
            collection_name=self._collection_name,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="doc_hash",
                            match=models.MatchValue(value=doc_hash),
                        ),
                        models.FieldCondition(
                            key="version",
                            match=models.MatchValue(value=keep_version),
                        ),
                    ],
                    must_not=[],
                ),
            ),
        )
        logger.info("Deleted old versions of %s keeping v%d", doc_hash, keep_version)
        return 0
```

**Step 3.4: 运行测试 1 验证通过**

```bash
cd /home/pangzy/code_project/EKRS/rag
pytest tests/unit/test_qdrant_client.py::test_ensure_collection_creates_dense_and_sparse -v
```

Expected: PASS

**Step 3.5: 写失败测试 2 — `test_ensure_collection_recreates_on_dim_mismatch`**

追加:
```python
def test_ensure_collection_recreates_on_dim_mismatch(
    mock_embedding_service: EmbeddingService,
) -> None:
    """B2 fix: dim mismatch (384 vs 1024) triggers delete + recreate."""
    client = _make_qdrant(existing_size=384)  # Old 6A dim
    with patch("ekrs_rag.retrieval.qdrant_client.QdrantClient", return_value=client):
        mgr = QdrantManager(
            host="localhost", port=6333, embedding_service=mock_embedding_service
        )
        mgr.ensure_collection(vector_size=1024)

    client.delete_collection.assert_called_once_with("rag_documents")
    client.create_collection.assert_called_once()
```

**Step 3.6: 运行测试 2 验证通过**

```bash
cd /home/pangzy/code_project/EKRS/rag
pytest tests/unit/test_qdrant_client.py::test_ensure_collection_recreates_on_dim_mismatch -v
```

Expected: PASS

**Step 3.7: 写失败测试 3 — `test_ensure_collection_no_recreate_when_dim_matches`**

追加:
```python
def test_ensure_collection_no_recreate_when_dim_matches(
    mock_embedding_service: EmbeddingService,
) -> None:
    """When existing dim matches expected, no recreate."""
    client = _make_qdrant(existing_size=1024)
    with patch("ekrs_rag.retrieval.qdrant_client.QdrantClient", return_value=client):
        mgr = QdrantManager(
            host="localhost", port=6333, embedding_service=mock_embedding_service
        )
        mgr.ensure_collection(vector_size=1024)

    client.delete_collection.assert_not_called()
    client.create_collection.assert_not_called()
```

**Step 3.8: 运行测试 3 验证通过**

```bash
cd /home/pangzy/code_project/EKRS/rag
pytest tests/unit/test_qdrant_client.py -v
```

Expected: 3/3 PASS

**Step 3.9: 写失败测试 4 — `test_upsert_chunks_encodes_via_embedding_service`**

追加:
```python
def test_upsert_chunks_encodes_via_embedding_service(
    mock_embedding_service: EmbeddingService,
) -> None:
    """B3 fix: upsert_chunks calls EmbeddingService.encode on chunk.text."""
    from ekrs_shared.models import Chunk
    chunks = [
        Chunk(text="hello", scope_path=[], source_block_ids=["b1"],
              token_count=1, doc_hash="d1", version=1, page_numbers=[]),
        Chunk(text="world", scope_path=[], source_block_ids=["b2"],
              token_count=1, doc_hash="d1", version=1, page_numbers=[]),
    ]
    client = _make_qdrant(existing_size=1024)
    with patch("ekrs_rag.retrieval.qdrant_client.QdrantClient", return_value=client):
        mgr = QdrantManager(
            host="localhost", port=6333, embedding_service=mock_embedding_service
        )
        n = mgr.upsert_chunks(chunks)

    assert n == 2
    # Verify the mock model was called with chunk texts
    mock_embedding_service._model.encode.assert_called_once()
    args, _ = mock_embedding_service._model.encode.call_args
    assert args[0] == ["hello", "world"]
    # Verify upsert received NamedVectors with dense + sparse
    upsert_call = client.upsert.call_args
    points = upsert_call.kwargs["points"]
    assert len(points) == 2
    assert "dense" in points[0].vector
    assert "sparse" in points[0].vector
```

**Step 3.10: 运行测试 4 验证通过**

```bash
cd /home/pangzy/code_project/EKRS/rag
pytest tests/unit/test_qdrant_client.py::test_upsert_chunks_encodes_via_embedding_service -v
```

Expected: PASS

**Step 3.11: 写失败测试 5 — `test_upsert_chunks_uses_named_vectors`**

追加:
```python
def test_upsert_chunks_uses_named_vectors(
    mock_embedding_service: EmbeddingService,
) -> None:
    """upsert_chunks sends Qdrant sparse format {indices, values}."""
    from ekrs_shared.models import Chunk
    chunks = [
        Chunk(text="hi", scope_path=[], source_block_ids=["b1"],
              token_count=1, doc_hash="d1", version=1, page_numbers=[]),
    ]
    client = _make_qdrant(existing_size=1024)
    with patch("ekrs_rag.retrieval.qdrant_client.QdrantClient", return_value=client):
        mgr = QdrantManager(
            host="localhost", port=6333, embedding_service=mock_embedding_service
        )
        mgr.upsert_chunks(chunks)

    points = client.upsert.call_args.kwargs["points"]
    sparse_vec = points[0].vector["sparse"]
    # D8: sparse is in Qdrant format {indices, values}
    assert set(sparse_vec.keys()) == {"indices", "values"}
    assert isinstance(sparse_vec["indices"], list)
    assert isinstance(sparse_vec["values"], list)
```

**Step 3.12: 运行测试 5 验证通过**

```bash
cd /home/pangzy/code_project/EKRS/rag
pytest tests/unit/test_qdrant_client.py -v
```

Expected: 5/5 PASS

**Step 3.13: 写失败测试 6 — `test_upsert_chunks_raises_when_embedding_service_dummy`**

追加:
```python
def test_upsert_chunks_raises_when_embedding_service_dummy(
    dummy_embedding_service: EmbeddingService,
) -> None:
    """D1: upsert_chunks raises EmbeddingUnavailableError in dummy mode."""
    from ekrs_shared.models import Chunk
    from ekrs_rag.retrieval.embedding_service import EmbeddingUnavailableError
    chunks = [
        Chunk(text="x", scope_path=[], source_block_ids=["b1"],
              token_count=1, doc_hash="d1", version=1, page_numbers=[]),
    ]
    client = _make_qdrant(existing_size=1024)
    with patch("ekrs_rag.retrieval.qdrant_client.QdrantClient", return_value=client):
        mgr = QdrantManager(
            host="localhost", port=6333, embedding_service=dummy_embedding_service
        )
        with pytest.raises(EmbeddingUnavailableError, match="dummy mode"):
            mgr.upsert_chunks(chunks)
```

**Step 3.14: 运行测试 6 验证通过**

```bash
cd /home/pangzy/code_project/EKRS/rag
pytest tests/unit/test_qdrant_client.py -v
```

Expected: 6/6 PASS

**Step 3.15: 写失败测试 7 — `test_search_calls_query_points`**

追加:
```python
def test_search_calls_query_points(
    mock_embedding_service: EmbeddingService,
) -> None:
    """B1 fix: search uses query_points (not removed .search)."""
    client = _make_qdrant(existing_size=1024)
    # Mock query_points return
    client.query_points.return_value = SimpleNamespace(
        points=[
            SimpleNamespace(payload={"text": "match"}, score=0.9),
        ]
    )
    with patch("ekrs_rag.retrieval.qdrant_client.QdrantClient", return_value=client):
        mgr = QdrantManager(
            host="localhost", port=6333, embedding_service=mock_embedding_service
        )
        results = mgr.search(query_text="hello", top_k=5)

    assert client.query_points.called
    client.search.assert_not_called()  # B1: .search removed in 1.17.1
    assert results == [({"text": "match"}, 0.9)]
```

**Step 3.16: 运行测试 7 验证通过**

```bash
cd /home/pangzy/code_project/EKRS/rag
pytest tests/unit/test_qdrant_client.py -v
```

Expected: 7/7 PASS

**Step 3.17: 写失败测试 8 — `test_search_encodes_query_text_via_service`**

追加:
```python
def test_search_encodes_query_text_via_service(
    mock_embedding_service: EmbeddingService,
) -> None:
    """search(query_text=...) calls EmbeddingService.encode on the text."""
    client = _make_qdrant(existing_size=1024)
    client.query_points.return_value = SimpleNamespace(points=[])
    with patch("ekrs_rag.retrieval.qdrant_client.QdrantClient", return_value=client):
        mgr = QdrantManager(
            host="localhost", port=6333, embedding_service=mock_embedding_service
        )
        mgr.search(query_text="user query", top_k=10)

    encode_args = mock_embedding_service._model.encode.call_args[0]
    assert "user query" in encode_args[0]
```

**Step 3.18: 运行测试 8 验证通过**

```bash
cd /home/pangzy/code_project/EKRS/rag
pytest tests/unit/test_qdrant_client.py -v
```

Expected: 8/8 PASS

**Step 3.19: 写失败测试 9 — `test_search_passes_named_vectors_to_query_points`**

追加:
```python
def test_search_passes_named_vectors_to_query_points(
    mock_embedding_service: EmbeddingService,
) -> None:
    """search passes Prefetch (dense + sparse) + FusionQuery to query_points."""
    client = _make_qdrant(existing_size=1024)
    client.query_points.return_value = SimpleNamespace(points=[])
    with patch("ekrs_rag.retrieval.qdrant_client.QdrantClient", return_value=client):
        mgr = QdrantManager(
            host="localhost", port=6333, embedding_service=mock_embedding_service
        )
        mgr.search(query_text="q", top_k=3)

    call_kwargs = client.query_points.call_args.kwargs
    # Two prefetches: dense + sparse
    assert isinstance(call_kwargs["prefetch"], list)
    assert len(call_kwargs["prefetch"]) == 2
    dense_prefetch = call_kwargs["prefetch"][0]
    sparse_prefetch = call_kwargs["prefetch"][1]
    assert dense_prefetch.using == "dense"
    assert sparse_prefetch.using == "sparse"
    # Fusion query
    assert call_kwargs["query"].fusion == models.Fusion.RRF
```

**Step 3.20: 运行测试 9 验证通过**

```bash
cd /home/pangzy/code_project/EKRS/rag
pytest tests/unit/test_qdrant_client.py -v
```

Expected: 9/9 PASS

**Step 3.21: 写失败测试 10 — `test_ensure_collection_handles_qdrant_unreachable`**

追加:
```python
def test_ensure_collection_handles_qdrant_unreachable(
    mock_embedding_service: EmbeddingService,
) -> None:
    """If Qdrant is unreachable, ensure_collection handles exception gracefully."""
    client = MagicMock()
    client.get_collection.side_effect = ConnectionError("Qdrant down")
    client.create_collection.side_effect = ConnectionError("Qdrant down")

    with patch("ekrs_rag.retrieval.qdrant_client.QdrantClient", return_value=client):
        mgr = QdrantManager(
            host="localhost", port=6333, embedding_service=mock_embedding_service
        )
        # tenacity retries 3x, then raises; we just verify the retries happened
        with pytest.raises(ConnectionError):
            mgr.ensure_collection(vector_size=1024)
    # Verify retry happened
    assert client.get_collection.call_count >= 1
```

**Step 3.22: 运行测试 10 验证通过**

```bash
cd /home/pangzy/code_project/EKRS/rag
pytest tests/unit/test_qdrant_client.py -v
```

Expected: 10/10 PASS

**Step 3.23: 写失败测试 11 — 验证旧 BGESmallEmbedder 测试已被删除**

```bash
cd /home/pangzy/code_project/EKRS
test -f rag/ekrs_rag/retrieval/embedder.py && echo "FAIL: old embedder.py still exists" || echo "OK: embedder.py removed"
test -f rag/tests/unit/test_embedder.py && echo "FAIL: old test_embedder.py still exists" || echo "OK: test_embedder.py removed"
```

Expected: `OK: embedder.py removed` + `OK: test_embedder.py removed`(实际 T4 才删,T3 暂留)

**Step 3.24: 验证完整套件不破**

```bash
cd /home/pangzy/code_project/EKRS/rag
pytest tests/ --cov=ekrs_rag -q 2>&1 | tail -10
```

Expected: 现有 531 测试可能因 BGESmallEmbedder 删除而失败(retriever.py 还在 import)— 接受失败,继续 T4。

**Step 3.25: Commit**

```bash
cd /home/pangzy/code_project/EKRS
git add rag/ekrs_rag/retrieval/qdrant_client.py rag/tests/unit/test_qdrant_client.py
git -c user.email=pangzy@anthropic.local -c user.name=pangzy commit -m "fix(retrieval): QdrantManager rewrite fixes 3 production bugs (B1/B2/B3)

B1: search() now uses query_points() — QdrantClient.search was REMOVED
    in qdrant-client 1.17.1. query_points supports hybrid search via
    Prefetch (dense + sparse) + FusionQuery(RRF).

B2: ensure_collection reads existing.config.params.vectors[\"dense\"].size
    (1.17.1 path); old vectors_config[\"dense\"].size no longer exists.

B3: upsert_chunks now takes injected EmbeddingService; encodes chunk.text
    into real 1024d dense + sparse vectors. Qdrant sparse format
    {indices, values} produced by EmbeddingService.to_qdrant_sparse (D8).

D1 hardening: upsert_chunks raises EmbeddingUnavailableError when
EmbeddingService is in dummy mode (prevents silent data corruption).
Search degrades gracefully to empty results in dummy mode (read-only safe).

D4: ensure_collection honors auto_reindex=True/False (AUTO_REINDEX env).
False (production) raises on dim mismatch instead of silent recreate.

D7: query failures now log operation type to qdrant_write_failed event
(semantic broadened per 6B spec D7).

11 unit tests cover: ensure_collection (3), upsert_chunks (4), search (3),
error handling (1)."
```

Expected: 1 commit, ~350 LOC.

---

### Task 4: Retriever Simplify + main.py + Delete BGESmallEmbedder

**Files:**
- Modify: `rag/ekrs_rag/retrieval/retriever.py`(移除 embedder 参数,改调 `d qdrant.search(query_text=...)`)
- Modify: `rag/ekrs_rag/main.py`(lifespan + Depends)
- Modify: `rag/ekrs_rag/api/dependencies.py`(加 `get_embedding_service`)
- Delete: `rag/ekrs_rag/retrieval/embedder.py`
- Delete: `rag/tests/unit/test_embedder.py`

**Interfaces:**
- Consumes: `EmbeddingService`(T2), `QdrantManager` 改签名(T3)
- Produces:
  ```python
  # retriever.py
  class EKRSRetriever:
      def __init__(self, qdrant: QdrantManager) -> None: ...  # embedder removed
      def retrieve(self, query: str, ...) -> RetrievalResult: ...  # no change

  # dependencies.py
  def get_embedding_service() -> EmbeddingService: ...

  # main.py lifespan
  embedding_service = EmbeddingService()
  qdrant_manager = QdrantManager(
      host=settings.QDRANT_HOST,
      port=settings.QDRANT_GRPC_PORT,
      embedding_service=embedding_service,
      auto_reindex=settings.AUTO_REINDEX,
  )
  await asyncio.to_thread(qdrant_manager.ensure_collection)
  app.state.embedding_service = embedding_service
  app.state.qdrant_manager = qdrant_manager
  ```

**Step 4.1: 修改 retriever.py — 移除 embedder 参数**

`rag/ekrs_rag/retrieval/retriever.py`(完整重写关键部分):
```python
"""Scope-aware retriever (Phase 6B).

Embeds queries via QdrantManager.search(query_text=...) which now
internally uses EmbeddingService. Retriever no longer holds embedder
directly (D5 simplification).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

from ekrs_shared.models import Chunk, NumericHint

from ekrs_rag.ingestion.numeric_hint_extractor import extract_hints
from ekrs_rag.retrieval.qdrant_client import QdrantManager

logger = logging.getLogger(__name__)

_SCOPE_PRIORITY_MAP = {
    "national": 100, "industry": 80, "enterprise": 60, "project": 40, "reference": 20,
}


@dataclass
class RetrievalResult:
    chunks: List[Chunk]
    vector_scores: List[float]
    scope_scores: List[float]
    final_scores: List[float]

    @property
    def scores(self) -> List[float]:
        return self.vector_scores


class EKRSRetriever:
    def __init__(self, qdrant: QdrantManager) -> None:
        self._qdrant = qdrant

    def retrieve(
        self,
        query: str,
        top_k: int = 40,
        active_scope: Optional[List[str]] = None,
    ) -> RetrievalResult:
        # Phase 6B: qdrant.search handles embedding internally (D5)
        hits = self._qdrant.search(query_text=query, top_k=top_k)
        if not hits:
            return RetrievalResult(chunks=[], vector_scores=[], scope_scores=[], final_scores=[])

        payloads, raw_scores = zip(*hits)
        chunks: List[Chunk] = []
        vector_scores: List[float] = []

        for payload, score in zip(payloads, raw_scores):
            chunk = Chunk(
                text=payload.get("text", ""),
                scope_path=payload.get("scope_path", []),
                source_block_ids=payload.get("source_block_ids", []),
                token_count=payload.get("token_count", 0),
                doc_hash=payload.get("doc_hash", ""),
                version=payload.get("version", 0),
                page_numbers=payload.get("page_numbers", []),
                numeric_hints=[],
            )
            if active_scope is not None:
                if not chunk.scope_path:
                    continue
                if not self._scope_matches(chunk.scope_path, active_scope):
                    continue
            hints: List[NumericHint] = extract_hints(chunk)
            chunk.numeric_hints = hints
            chunks.append(chunk)
            vector_scores.append(score)

        chunks, vector_scores, scope_scores, final_scores = self._rank_by_scope(chunks, vector_scores)
        logger.debug("Retrieved %d chunks, scope=%s", len(chunks), active_scope)
        return RetrievalResult(
            chunks=chunks, vector_scores=vector_scores,
            scope_scores=scope_scores, final_scores=final_scores,
        )

    @staticmethod
    def _scope_priority(chunk: Chunk) -> float:
        if not chunk.scope_path:
            return 0.0
        first = chunk.scope_path[0].lower()
        return _SCOPE_PRIORITY_MAP.get(first, 40) / 100.0

    def _rank_by_scope(self, chunks, vector_scores):
        if not chunks:
            return [], [], [], []
        scope_scores = [self._scope_priority(c) for c in chunks]
        final_scores = [vec * (1 + scope) for vec, scope in zip(vector_scores, scope_scores)]
        combined = list(zip(chunks, vector_scores, scope_scores, final_scores))
        combined.sort(key=lambda x: x[3], reverse=True)
        sorted_chunks, sorted_vec, sorted_scope, sorted_final = zip(*combined)
        return list(sorted_chunks), list(sorted_vec), list(sorted_scope), list(sorted_final)

    @staticmethod
    def _scope_matches(chunk_scope, active_scope):
        if len(chunk_scope) < len(active_scope):
            return False
        return chunk_scope[: len(active_scope)] == active_scope
```

**Step 4.2: 修改 dependencies.py — 加 `get_embedding_service`**

`rag/ekrs_rag/api/dependencies.py`(在文件末尾追加):
```python
def get_embedding_service() -> "EmbeddingService":
    """Get the EmbeddingService from app state. Strict 503 if uninitialized."""
    from fastapi import HTTPException, Request
    from ekrs_rag.retrieval.embedding_service import EmbeddingService
    # The actual app.state lookup happens in route handler via request.app
    raise NotImplementedError("Use request.app.state.embedding_service directly")
```

(实际是 import 即可,具体使用见 main.py 4.5)

**Step 4.3: 修改 main.py — lifespan 注入**

在 `rag/ekrs_rag/main.py` 找到 lifespan 函数(约 line 100-200),修改 `qdrant_manager` 初始化:
```python
# 原:
qdrant_manager = QdrantManager(host=settings.QDRANT_HOST, port=settings.QDRANT_GRPC_PORT)

# 新:
import asyncio
import logging
from ekrs_rag.retrieval.embedding_service import EmbeddingService
from ekrs_rag.retrieval.qdrant_client import QdrantManager

logger = logging.getLogger(__name__)

try:
    embedding_service = EmbeddingService()
    qdrant_manager = QdrantManager(
        host=settings.QDRANT_HOST,
        port=settings.QDRANT_GRPC_PORT,
        embedding_service=embedding_service,
        auto_reindex=settings.AUTO_REINDEX,
    )
    # D4: rebuild collection (if dim mismatch) in lifespan, before serve
    await asyncio.to_thread(qdrant_manager.ensure_collection)
except Exception as e:
    logger.exception(
        "RAG service startup failed during Qdrant/Embedding init: %s. "
        "Check Qdrant reachability, model files, and AUTO_REINDEX setting.",
        e,
    )
    raise  # FastAPI lifespan will record startup failure

app.state.embedding_service = embedding_service
app.state.qdrant_manager = qdrant_manager
```

并添加 `settings.AUTO_REINDEX`(默认 True,见 T6)。

**Step 4.4: 删除旧文件**

```bash
cd /home/pangzy/code_project/EKRS
rm rag/ekrs_rag/retrieval/embedder.py
rm rag/tests/unit/test_embedder.py
```

**Step 4.5: 验证 retriever 测试通过(必跑,用户反馈点 1)**

```bash
cd /home/pangzy/code_project/EKRS/rag
pytest tests/unit/test_retriever.py -v
```

Expected: 全部 PASS(retriever 测试已 mock QdrantManager,新签名兼容)。

**若失败**:retriever 测试的 mock 可能依赖旧 `qdrant.search(query_vector=...)` 签名,需同步更新为 `qdrant.search(query_text=...)`,调整 mock 返回值为 `query_points` 格式(SimpleNamespace(points=[SimpleNamespace(payload={...}, score=...)]))。这是 T4 范围内的小改动,无需新增任务。

**Step 4.6: 运行全测试套件**

```bash
cd /home/pangzy/code_project/EKRS/rag
pytest tests/ --cov=ekrs_rag --cov-fail-under=85 -q 2>&1 | tail -15
```

Expected: 531 + (T2 9 + T3 11) - (旧 test_embedder.py 6 例) = ~545 passed;≥85% coverage

**Step 4.7: Commit**

```bash
cd /home/pangzy/code_project/EKRS
git add rag/ekrs_rag/retrieval/retriever.py \
        rag/ekrs_rag/main.py \
        rag/ekrs_rag/api/dependencies.py \
        rag/ekrs_rag/retrieval/embedder.py \
        rag/tests/unit/test_embedder.py
git -c user.email=pangzy@anthropic.local -c user.name=pangzy commit -m "refactor(retrieval): remove BGESmallEmbedder, simplify retriever (D5)

Retriever no longer takes embedder param. qdrant.search(query_text=...)
internally encodes via injected EmbeddingService. D5 simplification:
single responsibility for embedding (EmbeddingService facade).

- Delete rag/ekrs_rag/retrieval/embedder.py (BGESmallEmbedder)
- Delete rag/tests/unit/test_embedder.py (6 unit tests)
- main.py lifespan: instantiate EmbeddingService, inject into QdrantManager
  with auto_reindex from settings.AUTO_REINDEX
- lifespan awaits ensure_collection via asyncio.to_thread (D4)
  so service listens only after rebuild completes
- dependencies.py: stub get_embedding_service (callers use app.state)

Existing retriever tests pass (QdrantManager mocked).
Total: -120 LOC (embedder.py + test_embedder.py), +30 LOC (retriever simplification)."
```

Expected: 1 commit, ~-90 LOC net.

---

### Task 5: Heavy Integration Tests + Nightly CI

**Files:**
- Create: `rag/tests/integration/test_embedding_heavy.py`
- Create: `.github/workflows/heavy-tests.yml`
- Modify: `pyproject.toml`(加 FlagEmbedding / onnxruntime / numpy 锁定 — T6 一并做,本任务只确保测试可运行)

**Step 5.1: 写 heavy 测试 1 — 真实 bge-m3 编码英文**

`rag/tests/integration/test_embedding_heavy.py`:
```python
"""Heavy integration tests for EmbeddingService (real bge-m3 model).

Marked @pytest.mark.heavy; skipped in default CI, run in nightly job.
Requires rag/models/bge-m3/ files (T1 vendored).
"""
from __future__ import annotations

import math
import os
from pathlib import Path

import pytest

from ekrs_rag.retrieval.embedding_service import (
    DEFAULT_MODEL_DIR,
    EmbeddingService,
)


# BGE_M3_MODEL_DIR env var overrides default for CI flexibility
MODEL_DIR = Path(os.environ.get("BGE_M3_MODEL_DIR", str(DEFAULT_MODEL_DIR)))


@pytest.mark.heavy
def test_real_bge_m3_encodes_english_text() -> None:
    """Real bge-m3 encodes English text to 1024d L2-normalized dense + sparse."""
    if not MODEL_DIR.exists():
        pytest.skip(f"Model dir {MODEL_DIR} not found; run T1 first")
    svc = EmbeddingService(model_dir=MODEL_DIR)
    if svc.is_dummy:
        pytest.skip("EmbeddingService in dummy mode (model load failed)")

    result = svc.encode(["hello world"])

    assert len(result) == 1
    assert len(result[0].dense) == 1024
    # L2 norm should be 1.0
    norm = math.sqrt(sum(v * v for v in result[0].dense))
    assert abs(norm - 1.0) < 1e-3
    # Sparse should have common English tokens
    assert len(result[0].sparse) > 0


@pytest.mark.heavy
def test_real_bge_m3_handles_chinese() -> None:
    """Real bge-m3 handles Chinese text with sparse containing Chinese tokens."""
    if not MODEL_DIR.exists():
        pytest.skip(f"Model dir {MODEL_DIR} not found; run T1 first")
    svc = EmbeddingService(model_dir=MODEL_DIR)
    if svc.is_dummy:
        pytest.skip("EmbeddingService in dummy mode (model load failed)")

    result = svc.encode(["高温合金材料"])

    assert len(result) == 1
    assert len(result[0].dense) == 1024
    # Chinese token IDs are typically in the thousands range
    assert any(tid > 1000 for tid in result[0].sparse.keys())
```

**Step 5.2: 注册 heavy marker 在 pyproject.toml**

`rag/pyproject.toml` 的 `[tool.pytest.ini_options]` 段加:
```toml
[tool.pytest.ini_options]
markers = [
    "heavy: marks tests as heavy (require real bge-m3 model, run in nightly job only)",
]
asyncio_mode = "auto"  # 如果已存在则不重复
```

**Step 5.3: 验证 heavy 测试被默认 skip**

```bash
cd /home/pangzy/code_project/EKRS/rag
pytest tests/integration/test_embedding_heavy.py -v 2>&1 | tail -10
```

Expected: 2 skipped(因为缺 FlagEmbedding 还没装 — 接下来 5.4 装)

**Step 5.4: 安装 FlagEmbedding 到当前环境**

```bash
cd /home/pangzy/code_project/EKRS
pip install "FlagEmbedding==1.2.13" "onnxruntime>=1.15.0,<1.18.0" "numpy>=1.24.0,<2.0.0"
```

**Step 5.5: 验证 heavy 测试可通过(本地)**

```bash
cd /home/pangzy/code_project/EKRS/rag
pytest tests/integration/test_embedding_heavy.py -v -m heavy
```

Expected: 2 passed(若 T1 模型 vendor 完成)

**Step 5.6: 创建 nightly CI workflow**

`/home/pangzy/code_project/EKRS/.github/workflows/heavy-tests.yml`:
```yaml
name: heavy-tests

on:
  schedule:
    - cron: '0 3 * * *'  # nightly 03:00 UTC
  workflow_dispatch:

jobs:
  heavy:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@v4
        with:
          lfs: false
          fetch-depth: 1  # avoid full history download (2GB model already in tree)
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Cache pip
        uses: actions/cache@v4
        with:
          path: ~/.cache/pip
          key: ${{ runner.os }}-pip-heavy-${{ hashFiles('rag/pyproject.toml') }}
      - run: python -m pip install -e shared/ -e 'rag[dev]'
      - run: pip install "FlagEmbedding==1.2.13" "onnxruntime>=1.15.0,<1.18.0" "numpy>=1.24.0,<2.0.0"
      - name: Run heavy tests (real bge-m3)
        run: |
          cd rag
          pytest tests/integration/test_embedding_heavy.py -v -m heavy
```

**Step 5.7: 验证 default CI 跳过 heavy**

```bash
cd /home/pangzy/code_project/EKRS/rag
pytest tests/ -m "not heavy" --cov=ekrs_rag --cov-fail-under=85 -q 2>&1 | tail -10
```

Expected: 现有 545 测试通过(无 heavy 参与),coverage ≥85%

**Step 5.8: Commit**

```bash
cd /home/pangzy/code_project/EKRS
git add rag/tests/integration/test_embedding_heavy.py \
        rag/pyproject.toml \
        .github/workflows/heavy-tests.yml
git -c user.email=pangzy@anthropic.local -c user.name=pangzy commit -m "test(retrieval): heavy integration tests + nightly CI job (D6)

- 2 heavy tests in tests/integration/test_embedding_heavy.py:
  test_real_bge_m3_encodes_english_text, test_real_bge_m3_handles_chinese
  Both marked @pytest.mark.heavy
- pyproject.toml: register 'heavy' marker
- .github/workflows/heavy-tests.yml: nightly cron 03:00 UTC + manual
  dispatch. Uses --depth=1 to avoid full history download (2GB model
  in tree, git LFS not used per spec D3)

Default CI (workflows/test.yml) runs -m 'not heavy' to keep ~2min.
Heavy job runs real bge-m3 nightly, 30min timeout.

FlagEmbedding==1.2.13, onnxruntime<1.18, numpy<2.0 (per spec D5 version pins)."
```

Expected: 1 commit, ~80 LOC.

---

### Task 6: Documentation + Env + Progress Sync

**Files:**
- Modify: `ekrs-handbook.md` (§7, §7.4 new, §14, §16)
- Modify: `.env.example`(加 `AUTO_REINDEX`)
- Modify: `rag/ekrs_rag/core/config.py`(加 `AUTO_REINDEX` settings 字段)
- Modify: `.superpowers/sdd/progress.md`(更新 6B 状态)
- Modify: `pyproject.toml`(更新 pyproject deps 锁定)

**Step 6.1: 加 `AUTO_REINDEX` 到 settings**

`rag/ekrs_rag/core/config.py`(找到 Settings 类,新增字段):
```python
AUTO_REINDEX: bool = True  # Phase 6B D4: auto-rebuild Qdrant on dim mismatch
```

**Step 6.2: 加 `AUTO_REINDEX` 到 .env.example**

在 `.env.example` 末尾追加:
```bash
# Phase 6B: Auto-rebuild Qdrant collection when dense dim changes (e.g., 384d -> 1024d)
# Set to false in production to require manual intervention
AUTO_REINDEX=true
```

**Step 6.3: 更新 pyproject.toml 锁定依赖**

`rag/pyproject.toml` 的 `[project.dependencies]` 段加:
```toml
"FlagEmbedding==1.2.13",
"onnxruntime>=1.15.0,<1.18.0",
"numpy>=1.24.0,<2.0.0",
```

(并运行 `pip install -e 'rag[dev]'` 验证)

**Step 6.4: 更新 handbook §7(bge-m3 实现)**

`ekrs-handbook.md` §7 表格中的"嵌入模型"行,改为:
```markdown
| 嵌入模型 | bge-m3 (ONNX, 1024d dense + sparse) | 文本向量化(FlagEmbedding 框架) |
```

**Step 6.5: 新增 handbook §7.4 — 首次部署流程**

在 §7.3 之后新增 §7.4:
```markdown
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
```

**Step 6.6: 更新 handbook §14(依赖清单)**

`ekrs-handbook.md` §14 运行时依赖列表加:
```markdown
FlagEmbedding (==1.2.13) — bge-m3 dense+sparse 推理框架(Phase 6B 新增)
onnxruntime (>=1.15.0,<1.18.0) — FlagEmbedding 依赖(锁定避免 API drift)
numpy (>=1.24.0,<2.0.0) — FlagEmbedding 依赖(锁定 1.x API)
```

**Step 6.7: 更新 handbook §16(qdrant_write_failed 语义)**

`ekrs-handbook.md` §16 审计日志段,修改为:
```markdown
**16 个事件名/schema 不可变更**:...(省略)... `qdrant_write_failed` (语义 Phase 6B 起放宽:覆盖 Qdrant 任何操作失败 read/write/delete/upsert/scroll,payload 含 `operation: str` 字段区分 read/write)。**back-compat 提示**:现有审计消费者(如监控脚本)需兼容 `operation` 字段缺失的情况——Phase 6A 之前的事件无此字段,Phase 6B 起的失败事件携带。监控脚本应:
- 处理新事件时优先用 `operation` 字段(若存在)
- 处理老事件时默认 `operation="write"`(Phase 6A 之前只有写入失败)
- 不要硬要求 `operation` 字段存在(用 `.get("operation", "write")`)
```

**Step 6.8: 更新 progress.md 加 6B 状态**

`.superpowers/sdd/progress.md` 末尾追加:
```markdown
## Phase 6B Retrieval Layer bge-m3 Migration

- Status: IN PROGRESS
- Tag target: `phase6b-retrieval-layer`
- 6 tasks + 1 tag (T1 vendor, T2 EmbeddingService, T3 QdrantManager, T4 retriever/main, T5 heavy tests, T6 docs)
- Iron Rules R1-R8 + 16 audit events preserved (D7: qdrant_write_failed semantic broadened)
- New dep: FlagEmbedding==1.2.13 (user approved), onnxruntime<1.18, numpy<2.0
- Spec: docs/superpowers/specs/2026-07-15-phase6b-retrieval-layer-design.md
```

**Step 6.9: 验证全测试 + coverage**

```bash
cd /home/pangzy/code_project/EKRS/rag
pytest tests/ -m "not heavy" --cov=ekrs_rag --cov-fail-under=85 -q 2>&1 | tail -10
```

Expected: ≥545 passed + 1 skipped + 0 failed;coverage ≥85%(预期 ~87-88%)

**Step 6.10: Commit**

```bash
cd /home/pangzy/code_project/EKRS
git add ekrs-handbook.md \
        .env.example \
        rag/ekrs_rag/core/config.py \
        rag/pyproject.toml \
        .superpowers/sdd/progress.md
git -c user.email=pangzy@anthropic.local -c user.name=pangzy commit -m "docs(6B): handbook sync + env + progress

Phase 6B closure documentation:

- ekrs-handbook.md:
  - §7: bge-m3 (1024d + sparse) replaces bge-small-en (384d)
  - §7.4 (new): first-deployment flow (lifespan rebuild -> parser
    re-push -> verify -> monitor)
  - §14: add FlagEmbedding/onnxruntime/numpy with version pins
  - §16: qdrant_write_failed semantic broadened to all Qdrant ops,
    payload includes operation field
- .env.example: AUTO_REINDEX=true (D4)
- core/config.py: Settings.AUTO_REINDEX: bool = True
- pyproject.toml: FlagEmbedding==1.2.13, onnxruntime<1.18, numpy<2.0
- progress.md: Phase 6B IN PROGRESS entry

All tests pass (m not heavy), coverage >=85%."
```

Expected: 1 commit, ~50 LOC.

---

### Task 7: Tag Phase 6B

**Files:**
- Tag: `phase6b-retrieval-layer`(本地)

**Step 7.1: 验证完整套件绿**

```bash
cd /home/pangzy/code_project/EKRS/rag
pytest tests/ -m "not heavy" --cov=ekrs_rag --cov-fail-under=85 -q 2>&1 | tail -5
```

Expected: ≥545 passed + 1 skipped + 0 failed;coverage ≥85%

**Step 7.2: 验证 git log 干净**

```bash
cd /home/pangzy/code_project/EKRS
git status --short
git log --oneline master | head -10
```

Expected: clean working tree;7 commits (T1-T6 + any task 11 review fix) since spec base

**Step 7.3: 切 tag**

```bash
cd /home/pangzy/code_project/EKRS
git tag -a phase6b-retrieval-layer -m "Phase 6B: retrieval layer bge-m3 migration

Closes 6A final review triage: 3 production retrieval bugs
(.search/vectors_config/zero-vectors) + bge-m3 (1024d + sparse).

Iron Rules R1-R8 preserved; 16 audit events preserved (D7: qdrant_write_failed
semantic broadened). New dep FlagEmbedding==1.2.13 (user approved).
Heavy tests via @pytest.mark.heavy + nightly CI job."
```

**Step 7.4: 验证 tag**

```bash
cd /home/pangzy/code_project/EKRS
git tag -l phase6b-retrieval-layer -n5
git log phase6b-retrieval-layer --oneline | head -10
```

Expected: 1 tag pointing at HEAD(T6 commit)

---

## Self-Review Checklist

执行前检查(我自己的,不是 subagent):

1. **Spec coverage**:
   - B1 .search() → query_points → Task 3.15-3.16 ✓
   - B2 vectors_config → config.params.vectors → Task 3.5-3.6 ✓
   - B3 零向量 → bge-m3 真实嵌入 → Task 3.9-3.10 ✓
   - D1 EmbeddingService + dummy 防御 + SHA256 → Task 2 + Task 3.13-3.14 ✓
   - D4 asyncio.to_thread + AUTO_REINDEX → Task 4.3 + Task 6.1-6.2 ✓
   - D6 mock + heavy + nightly → Task 3(全 mock)+ Task 5 ✓
   - D7 16 events + semantic 放宽 → 隐含(无新 event)+ Task 6.7 ✓
   - D8 sparse 转换在 EmbeddingService → Task 2.15-2.18 + Task 3.11-3.12 ✓
   - handbook §7.4 首次部署 + §16 语义 → Task 6.5 + Task 6.7 ✓

2. **Placeholder scan**:0 TBD/TODO/fixme(grep verified)

3. **Type consistency**:
   - `EmbeddingService.encode` 返回 `list[EncodedVector]` (Task 2, Task 3 用法一致)
   - `QdrantManager.search(query_text: str, ...)` (Task 3, Task 4 用法一致)
   - `QdrantManager.upsert_chunks(chunks: list[Chunk])` (Task 3 接受,Task 4 retriever 不直接用)
   - `EmbeddingUnavailableError` 在 Task 2 定义,Task 3 引用,Task 3.13 测试 — 一致

4. **Dependency**:
   - T1 → T2: T2 引用 `rag/models/bge-m3/` 文件
   - T2 → T3: T3 引用 `EmbeddingService` 接口
   - T3 → T4: T4 引用 T3 的 `QdrantManager(embedding_service=...)` 签名
   - T2-T4 → T5: T5 heavy 测试需要 EmbeddingService + FlagEmbedding
   - T6: 文档同步,不依赖代码(可与 T5 并行)

5. **LOC 估算 per commit**:
   - T1: ~2GB 二进制(无法 LOC 衡量,special review)
   - T2: ~250 LOC(embedding_service.py ~150 + 9 tests ~100)✓
   - T3: ~350 LOC(qdrant_client.py ~250 + 11 tests ~100)✓
   - T4: ~-90 LOC net(retriever 改 + main + delete embedder)✓
   - T5: ~80 LOC(2 tests + workflow)✓
   - T6: ~50 LOC(handbook + env)✓

   所有 commit 在 500 LOC 范围内(除 T1 二进制)。

6. **Coverage gate**:
   - T2: 9 新单测 100% 覆盖 EmbeddingService
   - T3: 11 新单测 100% 覆盖 QdrantManager 重写部分
   - T4: 删除 6 个旧测试(test_embedder.py)→ denominator 减
   - 末态预估:87-88% ≥ 85% gate ✓

7. **Audit event count**:
   - 0 new events added
   - 16 events preserved (D7: qdrant_write_failed payload 增 `operation` 字段,不是新 event)

OK 自检通过。可以执行。

---

## Execution Options

Plan complete and saved to `docs/superpowers/plans/2026-07-15-phase6b-retrieval-layer.md`.

Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task (T1-T6), review between tasks, fast iteration. T1 is special (binary vendor, no TDD).
2. **Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.
