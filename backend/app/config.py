from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(Path(__file__).parents[1] / ".env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "DJB 提链 API"
    app_env: str = "dev"
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"
    execution_mode: str = "legacy"
    legacy_api_url: str = "http://127.0.0.1:3000"
    database_url: str = "sqlite+aiosqlite:///./data/tilian.db"
    redis_url: str = ""
    kafka_bootstrap_servers: str = ""
    kafka_consumer_group: str = "tilian-dev"
    kafka_command_topic: str = "tilian.task.commands.v1"
    kafka_event_topic: str = "tilian.task.events.v1"
    kafka_dlq_topic: str = "tilian.dlq.v1"
    credential_encryption_key: str = ""
    owner_hash_secret: str = "replace-in-production"
    admin_api_token: str = ""
    auth_token_secret: str = ""
    user_token_ttl_seconds: int = 86400
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["http://127.0.0.1:5173", "http://localhost:5173"])
    auto_create_schema: bool = True
    request_timeout_seconds: float = 12.0
    task_lease_ttl_seconds: int = 120
    task_max_retries: int = 3
    # Reference UPI rebuilds a fresh Checkout/device/session context up to
    # five times. Other channels retain the general task retry budget.
    upi_max_retries: int = 5

    @field_validator("cors_origins", mode="before")
    @classmethod
    def split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("execution_mode")
    @classmethod
    def validate_mode(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"legacy", "distributed"}:
            raise ValueError("EXECUTION_MODE must be legacy or distributed")
        return normalized

    @property
    def kafka_enabled(self) -> bool:
        return bool(self.kafka_bootstrap_servers.strip())

    @property
    def redis_enabled(self) -> bool:
        return bool(self.redis_url.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
