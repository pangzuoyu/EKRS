"""EKRS RAG service configuration via environment variables."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """All env vars loaded from .env or environment."""

    # Auth
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

    @field_validator("PARSER_TOKEN")
    @classmethod
    def token_min_length(cls, v: str) -> str:
        if len(v) < 32:
            raise ValueError("PARSER_TOKEN must be >= 32 characters")
        return v

    @field_validator("SHARED_STORAGE_PATH")
    @classmethod
    def storage_must_be_absolute(cls, v: Path) -> Path:
        if not v.is_absolute():
            raise ValueError("SHARED_STORAGE_PATH must be an absolute path")
        return v

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


# Singleton
settings = Settings()
