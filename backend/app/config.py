"""Application settings loaded from the environment (spec section 5).

Startup fails with a clear validation error when a required value is missing
or invalid. No Keitaro calls are made from this module.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlparse

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

KEITARO_API_PATH_SUFFIX = "/admin_api/v1"

# The single dotenv file, anchored to the backend directory so the launch
# directory cannot change which file is loaded.
ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


class ConfigurationError(Exception):
    """Backend configuration is missing or invalid; startup must abort."""


def load_settings(**overrides: Any) -> Settings:
    """Load settings, failing startup with a message that names the broken
    fields without echoing any configured values (the environment may hold
    credentials).

    ``overrides`` go to the ``Settings`` constructor; tests use them to stay
    hermetic (e.g. ``_env_file=os.devnull`` to skip the dotenv file).
    """
    try:
        return Settings(**overrides)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        raise ConfigurationError(
            f"Invalid backend configuration: {problems}. "
            "Check environment variables and .env files (see backend/.env.example)."
        ) from None


class Settings(BaseSettings):
    """Backend configuration.

    Values are read from process environment variables first, then from
    ``backend/.env`` — the only dotenv file, resolved absolutely so the
    launch directory does not matter.
    """

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    keitaro_base_url: str
    keitaro_api_key: SecretStr
    keitaro_domain_id: int = Field(gt=0)
    keitaro_group_id: int = Field(gt=0)
    keitaro_traffic_source_id: int = Field(gt=0)
    google_redirect_url: str
    database_url: str

    keitaro_timeout_seconds: float = Field(default=30.0, gt=0)

    # Server-only Five API integration for offer autocomplete. Optional until
    # the /offers and /geos endpoints are implemented; requests to those
    # endpoints fail with 503 while unconfigured.
    five_api_base_url: str | None = None
    five_api_api_key: SecretStr | None = None
    five_api_timeout_seconds: float = Field(default=10.0, gt=0)

    cors_allowed_origins: Annotated[list[str], NoDecode] = ["http://localhost:5173"]
    log_level: str = "INFO"

    @field_validator("keitaro_base_url")
    @classmethod
    def _base_url_must_target_admin_api(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        if not normalized.endswith(KEITARO_API_PATH_SUFFIX):
            raise ValueError(
                f"KEITARO_BASE_URL must end with {KEITARO_API_PATH_SUFFIX!r}, got {value!r}"
            )
        parsed = urlparse(normalized)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("KEITARO_BASE_URL must be an absolute http(s) URL")
        return normalized

    @field_validator("google_redirect_url")
    @classmethod
    def _redirect_url_must_be_absolute_https(cls, value: str) -> str:
        normalized = value.strip()
        parsed = urlparse(normalized)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("GOOGLE_REDIRECT_URL must be an absolute https:// URL")
        return normalized

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _split_cors_origins(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    def secret_values(self) -> list[str]:
        """Values that must never appear in logs or responses."""
        secrets = [self.keitaro_api_key.get_secret_value()]
        if self.five_api_api_key is not None:
            secrets.append(self.five_api_api_key.get_secret_value())
        return [secret for secret in secrets if secret]
