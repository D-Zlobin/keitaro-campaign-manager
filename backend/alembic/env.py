"""Alembic migration environment.

The database URL comes from the application settings (``DATABASE_URL``) so
migrations always target the configured database; tests override it through
``config.set_main_option("sqlalchemy.url", ...)``. Online migrations run on
the production engine builder, so SQLite connections get the same foreign
keys and busy timeout as the application itself.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from app.config import load_settings
from app.db.engine import build_engine
from app.db.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = load_settings()
url = config.get_main_option("sqlalchemy.url") or settings.database_url
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = build_engine(url)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
