"""Alembic migration environment.

Reads :class:`~app.config.settings.DatabaseSettings` directly rather than
calling ``load_settings()``. That is deliberate: ``load_settings()`` also
validates the Meta credentials, and requiring a valid advertising access token
in order to run a database migration would block every deployment and CI run
that has no business holding one.

Importing :mod:`app.models` is what makes autogenerate work. Alembic compares
``Base.metadata`` against the live database, and metadata only contains tables
whose model modules have been imported.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

import app.models  # noqa: F401  (imported for its side effect: registering every table)
from app.config.settings import DatabaseSettings
from app.database.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Injected here rather than written into alembic.ini so the password is never
# committed and never duplicated away from DATABASE_URL.
_database_settings = DatabaseSettings()  # type: ignore[call-arg]
config.set_main_option("sqlalchemy.url", _database_settings.url)


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting to a database.

    Used to review or hand off DDL for a database the running process cannot
    reach, which is common where production migrations are applied by a DBA.
    """
    context.configure(
        url=_database_settings.url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against a live database connection.

    ``compare_type`` and ``compare_server_default`` are enabled so autogenerate
    notices a column whose type or default changed. Both are off by default in
    Alembic, which means a widened column silently produces an empty migration.
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
