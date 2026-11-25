"""Fixtures for persistence tests: per-test SQLite file with real migrations.

Every test gets its own file database migrated to head through the same
Alembic environment used in production, then an engine built by
``app.db.engine.build_engine`` so connection settings (foreign keys, busy
timeout) match production exactly.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.db.engine import build_engine, make_session_factory
from app.repositories.campaigns import CampaignRepository
from app.repositories.idempotency import IdempotencyRepository

BACKEND_ROOT = Path(__file__).resolve().parents[2]
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


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def campaigns() -> CampaignRepository:
    return CampaignRepository()


@pytest.fixture
def idempotency() -> IdempotencyRepository:
    return IdempotencyRepository()
