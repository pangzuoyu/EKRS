"""EKRS RAG service configuration via environment variables."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """All env vars loaded from .env or environment."""

    # Auth — placeholder default is intentionally invalid so the validator
    # rejects startup with the literal. Production MUST export PARSER_TOKEN
    # in .env (or process env) before boot; the lifespan startup check in
    # main.py is the authoritative fail-fast. The comment marker below is
    # the recognized invalid-default literal; do NOT change it without
    # updating the validator that pins against it.
    PARSER_TOKEN: str = "change-me-to-a-secure-random-string-32chars"

    # Qdrant
    QDRANT_HOST: str = "localhost"
    QDRANT_PORT: int = 6333
    QDRANT_GRPC_PORT: int = 6334
    COLLECTION_NAME: str = "rag_documents"

    # Redis
    REDIS_URL: str = "redis://localhost:6379"

    # Shared storage
    SHARED_STORAGE_PATH: Path = Path("/parsed_lib")

    # Debug
    EKRS_DEBUG: bool = False

    # Chunking
    MAX_CHUNK_TOKENS: int = 500

    # Recall gate threshold
    MIN_RECALL_CHUNKS: int = 1

    # Embedding model
    EMBEDDING_MODEL: str = "bge-small-en-v1.5"

    # Phase 6B: auto-rebuild Qdrant collection on dim mismatch (D4)
    AUTO_REINDEX: bool = True

    # Distributed lock
    INGESTION_LOCK_TIMEOUT: int = 300  # seconds

    # Phase 4: 分布式锁 & 任务表
    LOCK_TTL_SEC: int = 300
    MAX_ATTEMPTS: int = 3
    TASK_DB_PATH: str = "/var/lib/ekrs/tasks.db"

    # Phase 5: observability
    AUDIT_LOG_PATH: str = "audit.log"
    DEBUG_LOG_PATH: str = "logs/debug.log"

    # Server
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # Phase 6A: admin auth + parser callback
    ADMIN_KEY: str = ""  # empty = /calculate returns 503
    ENGINE_URL: str = "http://localhost:8000"
    # D8: independent DB path for spec §4 documents table trio
    # (decoupled from TASK_DB_PATH so the two repos can run on separate disks).
    DOCUMENTS_DB_PATH: str = "/var/lib/ekrs/documents.db"

    # Phase 6B: callback URL allowlist (T4 — SSRF mitigation)
    CALLBACK_ALLOWED_SCHEMES: str = "https"  # comma-separated
    CALLBACK_ALLOWED_HOSTS: str = ""  # comma-separated; "*" disables pinning

    # Pipeline / callback tuning (T6 — token header + retry semantics)
    PIPELINE_CALLBACK_MAX_ATTEMPTS: int = 3
    PIPELINE_RETRY_MIN_SEC: float = 2.0
    PIPELINE_RETRY_MAX_SEC: float = 10.0
    PIPELINE_CALLBACK_TIMEOUT_SEC: float = 30.0

    # P2: old-version cleanup switch (T11/T12)
    OLD_VERSION_DELETE_ENABLED: bool = True

    # Defensive cap on callback error payload (prevents oversized rows in
    # parser's parse_tasks.error column). Truncated server-side in _send_callback.
    CALLBACK_ERROR_MAX_CHARS: int = 1024

    @field_validator("PARSER_TOKEN")
    @classmethod
    def token_min_length(cls, v: str) -> str:
        if not v:
            raise ValueError(
                "PARSER_TOKEN is empty; set a 32+ character secret in .env"
            )
        if v == "change-me-to-a-secure-random-string-32chars":
            raise ValueError(
                "PARSER_TOKEN is the example default; replace with a real secret"
            )
        if len(v) < 32:
            raise ValueError("PARSER_TOKEN must be >= 32 characters")
        return v

    @field_validator("SHARED_STORAGE_PATH")
    @classmethod
    def storage_must_be_absolute(cls, v: Path) -> Path:
        if not v.is_absolute():
            raise ValueError("SHARED_STORAGE_PATH must be an absolute path")
        return v

    @field_validator("CALLBACK_ALLOWED_SCHEMES")
    @classmethod
    def validate_callback_schemes(cls, v: str) -> str:
        schemes = {s.strip().lower() for s in v.split(",") if s.strip()}
        if not schemes:
            raise ValueError("CALLBACK_ALLOWED_SCHEMES must contain at least one scheme")
        if not schemes.issubset({"http", "https"}):
            raise ValueError("CALLBACK_ALLOWED_SCHEMES only supports http and https")
        return v

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


def _fallback_settings() -> Settings:
    """Build a Settings via model_construct, bypassing validators.

    Used when a fresh Settings() raises (e.g., the PARSER_TOKEN placeholder
    default is invalid and no real .env was loaded). Values match the
    declared class defaults so attribute access is safe for modules that
    import `settings`. PARSER_TOKEN is forced to "" so the lifespan
    startup check refuses to boot until a real secret is provided via
    environment variable.
    """
    defaults: dict[str, Any] = {
        name: (info.default if info.default is not None else None)
        for name, info in Settings.model_fields.items()
    }
    defaults["PARSER_TOKEN"] = ""
    return Settings.model_construct(**defaults)


# Module-load singleton. Tolerates ValidationError so the module can be
# imported in environments without a real .env (test collections, REPL
# inspection, doc builds). The lifespan startup check in main.py raises
# RuntimeError when PARSER_TOKEN is missing/short, which is the
# authoritative fail-fast for production deploys. Fresh Settings() calls
# (e.g., tests in tests/unit/test_config.py) always run validators.
try:
    settings: Settings = Settings()
except ValidationError as exc:
    logger.warning(
        "Settings() failed validation at module load (likely placeholder "
        "PARSER_TOKEN and no real .env); using model_construct fallback. "
        "Lifespan will refuse to boot. Errors: %s",
        exc.errors(),
    )
    settings = _fallback_settings()
