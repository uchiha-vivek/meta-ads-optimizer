"""Engine construction, session factory, and the transaction boundary.

This module is the only place that calls ``create_engine``. There is no global
engine and no module-level session: both are built once in the CLI composition
root and injected into the repositories that need them, so a test can supply its
own engine without monkey-patching anything.

:func:`session_scope` defines where a transaction begins and ends. Repositories
deliberately do not commit; a service performing several repository calls needs
them to land atomically, and a repository that commits on its own makes that
impossible. The command owns the transaction, the service owns the work, and the
repository owns the SQL.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.config.settings import DatabaseSettings
from app.utils.exceptions import ConfigurationError, DatabaseError

_logger = logging.getLogger(__name__)


def create_database_engine(settings: DatabaseSettings) -> Engine:
    """Create the SQLAlchemy engine described by ``settings``.

    ``pool_pre_ping`` is enabled because the CLI is long-lived enough to hold
    pooled connections across an idle period, and PostgreSQL or an intermediary
    will eventually close one. Without the pre-ping the next command fails with
    a stale-connection error that looks like a database outage.

    Args:
        settings: Validated database configuration.

    Returns:
        A configured engine. No connection is opened until first use.

    Raises:
        ConfigurationError: If the URL is not a usable SQLAlchemy URL or names a
            driver that is not installed.
    """
    try:
        engine = create_engine(
            settings.url,
            echo=settings.echo_sql,
            pool_size=settings.pool_size,
            max_overflow=settings.max_overflow,
            pool_pre_ping=True,
        )
    except (ArgumentError, ImportError, SQLAlchemyError) as exc:
        raise ConfigurationError(
            "DATABASE_URL could not be used to build a database engine",
            context={"url": settings.safe_url, "reason": str(exc)},
        ) from exc

    _logger.debug(
        "Database engine created",
        extra={"database_url": settings.safe_url, "pool_size": settings.pool_size},
    )
    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Build the session factory bound to ``engine``.

    ``expire_on_commit`` is disabled so that ORM instances remain readable after
    the transaction commits. The CLI renders those instances into Rich tables
    after the service returns; with the default, every attribute access would
    trigger a refresh against a closed session.

    ``autoflush`` is disabled so that a read inside a service never silently
    writes pending changes; flushes happen where the code says they do.

    Args:
        engine: Engine the sessions connect through.

    Returns:
        A session factory ready to be injected into repositories.
    """
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@contextmanager
def session_scope(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """Provide a transactional session, committing on success.

    Any exception rolls the transaction back and propagates. SQLAlchemy failures
    are translated to :class:`~app.utils.exceptions.DatabaseError` so that
    callers above the repository layer never import SQLAlchemy to handle an
    error; application exceptions pass through untouched.

    Args:
        session_factory: Factory produced by :func:`create_session_factory`.

    Yields:
        An open session owning a transaction.

    Raises:
        DatabaseError: If the underlying driver or ORM raises.
    """
    session = session_factory()
    try:
        yield session
        session.commit()
    except SQLAlchemyError as exc:
        session.rollback()
        _logger.exception("Transaction rolled back after a database failure")
        raise DatabaseError(
            "Database transaction failed and was rolled back",
            context={"reason": str(exc)},
        ) from exc
    except Exception:
        session.rollback()
        _logger.exception("Transaction rolled back after an application failure")
        raise
    finally:
        session.close()
