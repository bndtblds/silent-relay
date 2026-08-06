from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from cryptography.fernet import Fernet
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "production"
    app_base_url: str = "https://localhost"
    app_timezone: str = "Europe/Berlin"
    database_url: str = "sqlite:///./data/app.db"
    field_encryption_key: str
    token_hmac_key: str = Field(min_length=32)
    fingerprint_hmac_key: str = Field(min_length=32)
    session_secret: str = Field(min_length=32)
    csrf_secret: str = Field(min_length=32)
    admin_username: str = "admin"
    admin_password_hash: str
    entitlement_provider: str = "allow_all"
    entitlement_provider_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    default_language: str = "en"
    delivery_max_attempts: int = 6
    hsts_enabled: bool = True
    trusted_proxy_count: int = 0
    log_level: str = "INFO"
    secure_cookies: bool = True
    session_ttl_minutes: int = 30
    scheduler_interval_seconds: int = 60
    rate_limit_window_seconds: int = 60
    rate_limit_default: int = 60
    rate_limit_login_window_seconds: int = 900
    rate_limit_login_attempts: int = 5
    rate_limit_account_creation_window_seconds: int = 3600
    rate_limit_account_creation_attempts: int = 3
    rate_limit_contact_confirmation_window_seconds: int = 3600
    rate_limit_contact_confirmation_attempts: int = 10
    rate_limit_notification_window_seconds: int = 3600
    rate_limit_notification_attempts: int = 10
    rate_limit_max_buckets: int = 50000

    @field_validator("default_language")
    @classmethod
    def validate_default_language(cls, value: str) -> str:
        language = value.strip().lower()
        if language not in {"de", "en"}:
            raise ValueError("DEFAULT_LANGUAGE must be 'de' or 'en'")
        return language

    @field_validator("entitlement_provider")
    @classmethod
    def validate_entitlement_provider(cls, value: str) -> str:
        provider = value.strip()
        if not provider:
            raise ValueError("ENTITLEMENT_PROVIDER must not be empty")
        return provider

    @field_validator("field_encryption_key")
    @classmethod
    def validate_field_key(cls, value: str) -> str:
        try:
            Fernet(value.encode())
        except (TypeError, ValueError) as exc:
            raise ValueError("FIELD_ENCRYPTION_KEY must be a valid Fernet key") from exc
        return value

    @model_validator(mode="after")
    def validate_production(self) -> "Settings":
        parsed = urlparse(self.app_base_url)
        if self.app_env == "production" and parsed.scheme != "https":
            raise ValueError("APP_BASE_URL must use HTTPS in production")
        if self.app_env == "production" and not self.admin_password_hash.startswith("$argon2id$"):
            raise ValueError("ADMIN_PASSWORD_HASH must be an Argon2id hash")
        if self.delivery_max_attempts < 1:
            raise ValueError("DELIVERY_MAX_ATTEMPTS must be positive")
        rate_limit_values = (
            self.rate_limit_window_seconds,
            self.rate_limit_default,
            self.rate_limit_login_window_seconds,
            self.rate_limit_login_attempts,
            self.rate_limit_account_creation_window_seconds,
            self.rate_limit_account_creation_attempts,
            self.rate_limit_contact_confirmation_window_seconds,
            self.rate_limit_contact_confirmation_attempts,
            self.rate_limit_notification_window_seconds,
            self.rate_limit_notification_attempts,
            self.rate_limit_max_buckets,
        )
        if any(value < 1 for value in rate_limit_values):
            raise ValueError("Rate-limit settings must be positive")
        return self

    def ensure_database_directory(self) -> None:
        prefix = "sqlite:///"
        if self.database_url.startswith(prefix):
            path = self.database_url.removeprefix(prefix)
            if path != ":memory:":
                Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
