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

    @field_validator("PARSER_TOKEN")
    @classmethod
    def token_min_length(cls, v: str) -> str:
        if len(v) < 32:
            raise ValueError("PARSER_TOKEN must be >= 32 characters")
        return v

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


# Singleton
settings = Settings()
