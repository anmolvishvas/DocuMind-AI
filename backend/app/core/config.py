"""Application configuration loaded from environment variables and ``.env``.

The settings object is immutable and created once per process via
:func:`get_settings`. Invalid or missing values raise :class:`ConfigurationError`
whose message lists field names and reasons only, never the offending values.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ASYNC_POSTGRES_PREFIX = "postgresql+asyncpg://"


class ConfigurationError(Exception):
    """Raised when application settings are missing or invalid."""


class Settings(BaseSettings):
    """Validated, immutable application settings.

    Values are read from process environment variables first, then from a
    ``.env`` file in the working directory. Variable names are case-insensitive.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # .env is shared with docker-compose (e.g. POSTGRES_* vars)
        frozen=True,
    )

    # --- General -------------------------------------------------------
    environment: Literal["development", "test", "production"] = Field(
        default="development", description="Deployment environment."
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO", description="Minimum log level."
    )
    cors_origins: list[str] = Field(
        default=["http://localhost:3000", "http://localhost:5173"],
        description="Allowed CORS origins, as a JSON list.",
    )

    # --- Database ------------------------------------------------------
    database_url: SecretStr = Field(
        description="Async SQLAlchemy URL, e.g. postgresql+asyncpg://user:pass@host:5432/db."
    )
    db_pool_size: int = Field(default=10, ge=1, le=100, description="Connection pool size.")
    db_max_overflow: int = Field(default=10, ge=0, le=100, description="Extra pool connections.")
    db_pool_timeout: float = Field(default=30.0, gt=0, description="Pool checkout timeout (s).")

    # --- OpenAI --------------------------------------------------------
    openai_api_key: SecretStr = Field(min_length=1, description="OpenAI API key.")
    openai_timeout: float = Field(default=30.0, gt=0, description="OpenAI request timeout (s).")
    embedding_model: str = Field(default="text-embedding-3-small", min_length=1)
    embedding_dimensions: int = Field(default=1536, gt=0)
    embedding_batch_size: int = Field(
        default=64, ge=1, le=64, description="Chunks per embedding request (max 64)."
    )
    chat_model: str = Field(default="gpt-4o-mini", min_length=1)

    # --- Retrieval & ingestion ----------------------------------------
    retrieval_top_k: int = Field(default=5, ge=1, le=50)
    similarity_threshold: float = Field(
        default=0.70, ge=0.0, le=1.0, description="Minimum cosine similarity."
    )
    max_upload_size_mb: int = Field(default=20, ge=1, le=20, description="Upload cap (MB).")
    document_storage_dir: Path = Field(
        default=Path("storage/documents"), description="Directory for staged uploads."
    )

    @field_validator("database_url", mode="after")
    @classmethod
    def _require_asyncpg_url(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith(_ASYNC_POSTGRES_PREFIX):
            msg = f"must start with '{_ASYNC_POSTGRES_PREFIX}'"
            raise ValueError(msg)
        return value

    @property
    def max_upload_size_bytes(self) -> int:
        return self.max_upload_size_mb * 1024 * 1024


def _format_validation_error(error: ValidationError) -> str:
    """Summarise a validation error without echoing any input values."""
    lines = [
        f"  - {'.'.join(str(part) for part in item['loc']).upper() or 'settings'}: {item['msg']}"
        for item in error.errors(include_input=False, include_url=False)
    ]
    return "Invalid application configuration:\n" + "\n".join(lines)


def load_settings() -> Settings:
    """Build a fresh :class:`Settings`, translating failures to ConfigurationError."""
    try:
        return Settings()
    except ValidationError as exc:
        raise ConfigurationError(_format_validation_error(exc)) from None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings instance, creating it on first use."""
    return load_settings()
