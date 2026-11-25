"""Database engine and session management (spec section 4.1, plan stage 1).

SQLite is the supported database. Every connection created through
:func:`build_engine` runs with foreign-key enforcement enabled and a busy
timeout for short lock contention — the same settings in production and in
tests. In-memory databases share a single pooled connection so the PRAGMAs
and transactional behavior match file databases.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

SQLITE_BUSY_TIMEOUT_MS = 5000


def build_engine(database_url: str, *, busy_timeout_ms: int = SQLITE_BUSY_TIMEOUT_MS) -> Engine:
    """Create an engine with SQLite connection settings applied on connect."""
    kwargs: dict[str, Any] = {}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if ":memory:" in database_url:
            kwargs["poolclass"] = sa.pool.StaticPool

    engine = sa.create_engine(database_url, **kwargs)

    @event.listens_for(engine, "connect")
    def _configure_sqlite_connection(dbapi_connection: Any, _: Any) -> None:
        if engine.dialect.name != "sqlite":
            return
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        cursor.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Run a block in one committed transaction, rolling back on any failure."""
    session = factory()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
