"""Server-only provider configuration. Never return credentials to the browser."""

from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parent.parent / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )
    api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("DEEPSEEK_API_KEY", "LLM_API_KEY")
    )
    amap_api_key: SecretStr | None = None
    base_url: str = Field(default="https://api.deepseek.com", validation_alias="LLM_BASE_URL")
    model: str = Field(default="deepseek-flash", validation_alias="LLM_MODEL")
    planner_effort: Literal["low", "high", "max"] = Field(
        default="max", validation_alias="TRIPWEAVE_PLANNER_EFFORT"
    )
    research_effort: Literal["low", "high", "max"] = Field(
        default="high", validation_alias="TRIPWEAVE_RESEARCH_EFFORT"
    )
    reviewer_effort: Literal["low", "high", "max"] = Field(
        default="max", validation_alias="TRIPWEAVE_REVIEWER_EFFORT"
    )
    run_timeout: int = Field(default=180, ge=5, le=180, validation_alias="TRIPWEAVE_RUN_TIMEOUT")
    max_requests: int = Field(default=8, ge=2, le=10, validation_alias="TRIPWEAVE_MAX_REQUESTS")
    planner_max_tokens: int = Field(
        default=6000, ge=1, le=393216, validation_alias="TRIPWEAVE_PLANNER_MAX_TOKENS"
    )
    research_max_tokens: int = Field(
        default=4000, ge=1, le=393216, validation_alias="TRIPWEAVE_RESEARCH_MAX_TOKENS"
    )
    reviewer_max_tokens: int = Field(
        default=6000, ge=1, le=393216, validation_alias="TRIPWEAVE_REVIEWER_MAX_TOKENS"
    )
    database: str = Field(
        default=".runtime/tripweave.sqlite3", validation_alias="TRIPWEAVE_DATABASE"
    )
    database_url: SecretStr | None = Field(default=None, validation_alias="DATABASE_URL")

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.api_key.get_secret_value().strip())
