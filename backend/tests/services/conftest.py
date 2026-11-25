"""Fixtures for application-service tests: real SQLite, fake Keitaro."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.services.campaigns import CampaignService
from tests.fakes import FakeKeitaroAdapter


@pytest.fixture
def fake_keitaro() -> FakeKeitaroAdapter:
    return FakeKeitaroAdapter()


@pytest.fixture
def service(
    session_factory: sessionmaker[Session], fake_keitaro: FakeKeitaroAdapter, settings: Settings
) -> CampaignService:
    return CampaignService(session_factory=session_factory, keitaro=fake_keitaro, settings=settings)
