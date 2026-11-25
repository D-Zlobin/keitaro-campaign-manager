"""Migration tests: upgrade/downgrade against a real SQLite file."""

from __future__ import annotations

import sqlalchemy as sa

from tests.db.conftest import downgrade_database, upgrade_database

EXPECTED_TABLES = {
    "campaigns",
    "streams",
    "stream_offers",
    "baseline_snapshots",
    "idempotency_records",
}


def _tables(database_url: str) -> set[str]:
    engine = sa.create_engine(database_url)
    try:
        return set(sa.inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_upgrade_head_creates_all_tables(database_url: str) -> None:
    upgrade_database(database_url)
    assert EXPECTED_TABLES <= _tables(database_url)


def test_upgrade_then_downgrade_base_removes_all_tables(database_url: str) -> None:
    upgrade_database(database_url)
    downgrade_database(database_url, "base")
    # Only Alembic's own bookkeeping table remains.
    assert _tables(database_url).isdisjoint(EXPECTED_TABLES)
    assert _tables(database_url) == {"alembic_version"}


def test_upgrade_head_twice_is_a_noop(database_url: str) -> None:
    upgrade_database(database_url)
    upgrade_database(database_url)
    assert EXPECTED_TABLES <= _tables(database_url)


def test_migrated_database_accepts_production_connection_settings(
    migrated_database_url: str,
) -> None:
    """After migrating, an engine built the production way sees the schema and
    applies foreign keys / busy timeout on its connections."""
    from app.db.engine import SQLITE_BUSY_TIMEOUT_MS, build_engine

    engine = build_engine(migrated_database_url)
    try:
        with engine.connect() as connection:
            assert connection.execute(sa.text("PRAGMA foreign_keys")).scalar_one() == 1
            assert connection.execute(sa.text("PRAGMA busy_timeout")).scalar_one() == (
                SQLITE_BUSY_TIMEOUT_MS
            )
            tables = set(sa.inspect(connection).get_table_names())
            assert EXPECTED_TABLES <= tables
    finally:
        engine.dispose()
