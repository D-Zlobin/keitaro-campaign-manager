import os

import pytest
from pydantic import ValidationError

from app.config import Settings

REQUIRED_ENV_NAMES = [
    "KEITARO_BASE_URL",
    "KEITARO_API_KEY",
    "KEITARO_DOMAIN_ID",
    "KEITARO_GROUP_ID",
    "KEITARO_TRAFFIC_SOURCE_ID",
    "GOOGLE_REDIRECT_URL",
    "DATABASE_URL",
]


def _clear_required(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in REQUIRED_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_missing_required_settings_fail_startup_clearly(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_required(monkeypatch)
    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=os.devnull)
    message = str(exc_info.value)
    for field in (
        "keitaro_base_url",
        "keitaro_api_key",
        "keitaro_domain_id",
        "keitaro_group_id",
        "keitaro_traffic_source_id",
        "google_redirect_url",
        "database_url",
    ):
        assert field in message


def test_defaults(settings: Settings) -> None:
    assert settings.keitaro_timeout_seconds == 30.0
    assert settings.five_api_base_url is None
    assert settings.five_api_api_key is None
    assert settings.cors_allowed_origins == ["http://localhost:5173"]


@pytest.mark.parametrize("value", [0, -5])
def test_ids_must_be_positive(value: int) -> None:
    with pytest.raises(ValidationError):
        Settings(keitaro_domain_id=value)
    with pytest.raises(ValidationError):
        Settings(keitaro_group_id=value)
    with pytest.raises(ValidationError):
        Settings(keitaro_traffic_source_id=value)


def test_base_url_must_end_with_admin_api_path() -> None:
    with pytest.raises(ValidationError, match="admin_api"):
        Settings(keitaro_base_url="https://tracker.example.com")


def test_base_url_trailing_slash_is_normalized() -> None:
    settings = Settings(keitaro_base_url="https://tracker.example.com/admin_api/v1/")
    assert settings.keitaro_base_url == "https://tracker.example.com/admin_api/v1"


@pytest.mark.parametrize("value", ["http://www.google.com/", "not-a-url", "ftp://x/"])
def test_redirect_url_must_be_absolute_https(value: str) -> None:
    with pytest.raises(ValidationError, match="https"):
        Settings(google_redirect_url=value)


def test_api_key_is_secret_and_hidden_from_repr(settings: Settings) -> None:
    assert settings.keitaro_api_key.get_secret_value() == "test-api-key"
    assert "test-api-key" not in repr(settings)
    assert "test-api-key" not in str(settings)


def test_cors_origins_parse_comma_separated_string() -> None:
    settings = Settings(cors_allowed_origins="https://a.example, https://b.example")
    assert settings.cors_allowed_origins == ["https://a.example", "https://b.example"]


def test_secret_values_lists_all_configured_credentials() -> None:
    settings = Settings(five_api_api_key="five-secret")
    assert settings.secret_values() == ["test-api-key", "five-secret"]


def test_load_settings_error_names_fields_without_echoing_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import ConfigurationError, load_settings

    _clear_required(monkeypatch)
    monkeypatch.setenv("KEITARO_API_KEY", "real-secret-key-value")
    # Skip the developer's real backend/.env so only the process environment
    # above feeds the settings.
    with pytest.raises(ConfigurationError) as exc_info:
        load_settings(_env_file=os.devnull)
    message = str(exc_info.value)
    assert "keitaro_base_url" in message
    assert "database_url" in message
    assert "real-secret-key-value" not in message
