from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../../.env"),
        env_prefix="CALENDAR_",
        extra="ignore",
    )

    app_name: str = "Trade Calendar API"
    environment: str = "development"
    database_url: str = "sqlite+aiosqlite:///./calendar.db"
    user_timezone: str = "Asia/Shanghai"
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])
    log_level: str = "INFO"
    session_secret: SecretStr = SecretStr("development-only-change-me")
    # Shared secret required on every API route except /health, /ready and
    # /calendar/* once it is configured. This is the gate that makes it safe to
    # expose the API through a public Cloudflare Tunnel: without the matching
    # X-Internal-Api-Secret header the API answers 401, so the tunnel hostname
    # is useless to anyone who does not also hold the secret. Leave it unset to
    # disable the gate (local development, unit tests).
    internal_api_secret: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "CALENDAR_INTERNAL_API_SECRET", "INTERNAL_API_SECRET"
        ),
    )
    feishu_webhook_url: SecretStr | None = None
    finnhub_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("CALENDAR_FINNHUB_API_KEY", "FINNHUB_API_KEY"),
    )
    agnes_api_key: SecretStr | None = None
    agnes_model: str = "agnes-2.5-flash"
    ics_token: SecretStr = SecretStr("development-ics-token")
    config_dir: Path = Path("../../config")
    public_base_url: str = "http://localhost:3000"


@lru_cache
def get_settings() -> Settings:
    return Settings()
