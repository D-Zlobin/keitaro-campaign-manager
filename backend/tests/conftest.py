"""Test environment: deterministic, secret-free settings.

Process environment variables take precedence over any local ``.env`` file,
so the values below isolate the test suite from a developer's real
configuration.
"""

import os

os.environ.setdefault("KEITARO_BASE_URL", "https://keitaro-test.invalid/admin_api/v1")
os.environ.setdefault("KEITARO_API_KEY", "test-api-key")
os.environ.setdefault("KEITARO_DOMAIN_ID", "1813")
os.environ.setdefault("KEITARO_GROUP_ID", "10332")
os.environ.setdefault("KEITARO_TRAFFIC_SOURCE_ID", "836")
os.environ.setdefault("GOOGLE_REDIRECT_URL", "https://www.google.com/")
os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))
