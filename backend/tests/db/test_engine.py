"""SQLite connection settings: foreign keys and busy timeout on every connect."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db.engine import SQLITE_BUSY_TIMEOUT_MS, build_engine


def _pragma(engine, name: str) -> int:
    with engine.connect() as connection:
        return connection.execute(text(f"PRAGMA {name}")).scalar_one()


def test_foreign_keys_enabled_on_every_connection(engine) -> None:
    assert _pragma(engine, "foreign_keys") == 1


def test_busy_timeout_set_on_every_connection(engine) -> None:
    assert _pragma(engine, "busy_timeout") == SQLITE_BUSY_TIMEOUT_MS


def test_busy_timeout_is_configurable(database_url: str) -> None:
    custom = build_engine(database_url, busy_timeout_ms=250)
    try:
        assert _pragma(custom, "busy_timeout") == 250
    finally:
        custom.dispose()


def test_foreign_keys_enforced_for_raw_inserts(migrated_database_url: str) -> None:
    """A connection that never goes through the ORM still enforces FKs."""
    engine = build_engine(migrated_database_url)
    try:
        with engine.begin() as connection:
            with pytest.raises(IntegrityError, match="FOREIGN KEY"):
                connection.execute(
                    text(
                        "INSERT INTO streams (id, campaign_id, keitaro_id, name, type, "
                        '"schema", position, state, share, baseline_payload, '
                        "created_at, updated_at) VALUES (randomblob(16), randomblob(16), "
                        "1, 's', 'regular', 'landings', 0, 'active', 50, '{}', "
                        "'2026-01-01', '2026-01-01')"
                    )
                )
    finally:
        engine.dispose()


def test_model_metadata_create_all_matches_migrated_schema(engine) -> None:
    """Running the models' DDL over a migrated database is a no-op."""
    from sqlalchemy import inspect

    from app.db.models import Base

    inspector = inspect(engine)
    before = inspector.get_table_names()
    Base.metadata.create_all(engine)
    assert inspect(engine).get_table_names() == before
