"""Test environment: deterministic, secret-free settings.

Process environment variables take precedence over any local ``.env`` file,
so the values below isolate the test suite from a developer's real
configuration.

Also hosts the shared database fixtures: every test gets its own SQLite file
migrated to head through the same Alembic environment used in production,
then an engine built by ``app.db.engine.build_engine`` so connection settings
(foreign keys, busy timeout) match production exactly.
"""

import os
from collections.abc import Iterator
from pathlib import Path

os.environ.setdefault("KEITARO_BASE_URL", "https://keitaro-test.invalid/admin_api/v1")
os.environ.setdefault("KEITARO_API_KEY", "test-api-key")
os.environ.setdefault("KEITARO_DOMAIN_ID", "1813")
os.environ.setdefault("KEITARO_GROUP_ID", "10332")
os.environ.setdefault("KEITARO_TRAFFIC_SOURCE_ID", "836")
os.environ.setdefault("GOOGLE_REDIRECT_URL", "https://www.google.com/")
os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.config import Settings
from app.db.engine import build_engine, make_session_factory
from app.main import create_app

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"


def alembic_config(database_url: str) -> Config:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def upgrade_database(database_url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(database_url), revision)


def downgrade_database(database_url: str, revision: str) -> None:
    command.downgrade(alembic_config(database_url), revision)


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path}/test.db"


@pytest.fixture
def migrated_database_url(database_url: str) -> str:
    upgrade_database(database_url)
    return database_url


@pytest.fixture
def engine(migrated_database_url: str) -> Iterator[Engine]:
    engine = build_engine(migrated_database_url)
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return make_session_factory(engine)
