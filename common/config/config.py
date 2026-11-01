from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    log_level: str = "INFO"

    s3_endpoint_url: str | None = None
    s3_region: str = "us-east-1"
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    s3_bucket: str = "documents"
    s3_force_path_style: bool = False

    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    jwt_expires_in: int = 3600

    database_url: str = "postgresql+asyncpg://rve:rve@localhost:5433/rve"

    max_upload_bytes: int = 25 * 1024 * 1024
    max_uncompressed_bytes: int = 200 * 1024 * 1024
    max_upload_files: int = 50
    max_batch_bytes: int = 200 * 1024 * 1024

    rabbitmq_url: str = "amqp://rve:rve@localhost:5672/"
    ingestion_queue: str = "ingestion.jobs"
    document_events_queue: str = "document.events"
    consumer_prefetch: int = 10
    consume_document_events: bool = True

    chunk_size: int = 1000
    chunk_overlap: int = 150

    embedding_provider: str = "gemini"
    embedding_model: str | None = None
    embedding_dimensions: int = 768
    embedding_batch_size: int = 100
    gemini_api_key: str | None = None

    llm_provider: str = "gemini"
    llm_model: str | None = None
    llm_temperature: float = 0.2

    retrieval_top_k: int = 5
    retrieval_min_score: float = 0.65
    chat_max_query_chars: int = 2000
    chat_ticket_expires_in: int = 60

    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str | None = None
    qdrant_collection: str = "document_chunks"

    @field_validator(
        "s3_endpoint_url",
        "s3_access_key_id",
        "s3_secret_access_key",
        "embedding_model",
        "llm_model",
        "gemini_api_key",
        "qdrant_api_key",
        mode="before",
    )
    @classmethod
    def _empty_to_none(cls, value: str | None) -> str | None:
        return value or None


@lru_cache
def get_settings() -> Settings:
    return Settings()
