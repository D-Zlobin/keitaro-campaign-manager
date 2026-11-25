"""Persistence-test fixtures.

The database fixtures and Alembic helpers live in the root
``tests/conftest.py`` so service and API tests share them; they are
re-exported here for the existing ``tests.db`` imports.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.repositories.campaigns import CampaignRepository
from app.repositories.idempotency import IdempotencyRepository
from tests.conftest import (  # noqa: F401  (re-exported for tests.db modules)
    alembic_config,
    downgrade_database,
    upgrade_database,
)


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
