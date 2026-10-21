from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", ".env.langfuse"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "llm-convo-platform"
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/llm_convo"
    database_auto_create: bool = True
    log_level: str = "INFO"

    worker_poll_seconds: float = Field(default=2.0, gt=0)
    worker_lease_seconds: int = Field(default=180, ge=30)
    worker_heartbeat_seconds: int = Field(default=30, ge=5)
    max_concurrent_conversations: int = Field(default=10, ge=1, le=1000)
    shutdown_grace_seconds: float = Field(default=10.0, ge=0)

    vm_master_base_url: str | None = None
    vm_master_evaluator_token: str | None = None
    vm_master_sandbox_token: str | None = None
    vm_master_request_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    vm_master_job_poll_seconds: float = Field(default=1.0, gt=0, le=30)
    vm_master_job_timeout_seconds: float = Field(default=1_200.0, gt=0, le=14_400)

    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_base_url: str | None = None
    langfuse_public_url: str | None = None
    langfuse_project_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("LANGFUSE_PROJECT_ID", "LANGFUSE_INIT_PROJECT_ID"),
    )
    langfuse_tracing_environment: str = "development"


@lru_cache
def get_settings() -> Settings:
    return Settings()
