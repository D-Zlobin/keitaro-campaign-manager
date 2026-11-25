"""API tests: real FastAPI app with a migrated database and fake Keitaro."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.fakes import FakeKeitaroAdapter


@pytest.fixture
def fake_keitaro() -> FakeKeitaroAdapter:
    return FakeKeitaroAdapter()


@pytest.fixture
def api_settings(migrated_database_url: str) -> Settings:
    return Settings(database_url=migrated_database_url)


@pytest.fixture
def api_client(api_settings: Settings, fake_keitaro: FakeKeitaroAdapter) -> TestClient:
    return TestClient(create_app(api_settings, keitaro=fake_keitaro))
