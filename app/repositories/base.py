"""Shared persistence primitives for the repository layer.

:class:`EntityStore` provides the mechanics every repository needs — insert,
primary-key lookup, delete, count, statement execution, and the translation of
SQLAlchemy failures into :class:`~app.utils.exceptions.RepositoryError`.

Repositories **contain** a store rather than inheriting from one. Inheritance
would give every repository the full CRUD surface whether or not it makes sense:
an insights repository has no meaningful ``delete_by_id``, and inheriting one
only to leave it unused invites callers to reach for it. Composition lets each
repository publish exactly the operations its aggregate supports, which is what
keeps the public surface honest.

No method here commits. The transaction boundary is
:func:`~app.database.session.session_scope`, owned by the caller, because a
service writing through several repositories needs those writes to land as one
unit.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any, Final

from sqlalchemy import Select, func, inspect, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.database.base import Base
from app.utils.exceptions import EntityNotFoundError, RepositoryError

_logger = logging.getLogger(__name__)

# Columns never carried over during an upsert. The primary key identifies the
# stored row, not the incoming one, and the timestamps are managed by the
# database.
UPSERT_EXCLUDED_COLUMNS: Final[frozenset[str]] = frozenset({"id", "created_at", "updated_at"})


@contextmanager
def translate_database_errors(operation: str, **context: object) -> Iterator[None]:
    """Convert SQLAlchemy failures into :class:`RepositoryError`.

    Layers above the repository must be able to handle a persistence failure
    without importing SQLAlchemy. Failures are logged here, where the operation
    name and its context are still known, rather than at the boundary where they
    surface stripped of meaning.

    Args:
        operation: Human-readable name of the work being attempted.
        **context: Structured detail attached to the log record and exception.

    Yields:
        Control to the guarded block.

    Raises:
        RepositoryError: If the guarded block raises ``SQLAlchemyError``.
    """
    try:
        yield
    except SQLAlchemyError as exc:
        _logger.error(
            "Repository operation failed: %s",
            operation,
            extra={"operation": operation, **context},
            exc_info=True,
        )
        raise RepositoryError(
            f"Database operation failed: {operation}",
            context={**context, "reason": str(exc)},
        ) from exc


def copy_scalar_columns(
    *,
    source: Base,
    target: Base,
    exclude: frozenset[str] = UPSERT_EXCLUDED_COLUMNS,
) -> None:
    """Copy mapped scalar columns from ``source`` onto ``target``.

    Used to refresh a stored row from a freshly fetched one during an upsert.
    The column list is derived from the mapper rather than hand-written, so a
    column added to a model is carried over without anyone remembering to update
    a copy routine — the failure mode being a field that silently stops
    updating.

    Relationships are not touched: rewriting a collection here would cascade
    deletes across children that the incoming, partially populated object simply
    had not loaded.

    Args:
        source: Newly built instance holding current values.
        target: Persistent instance to update in place.
        exclude: Column attribute names to leave untouched.

    Raises:
        RepositoryError: If the two instances are not the same mapped class.
    """
    if type(source) is not type(target):
        raise RepositoryError(
            "Cannot copy columns between different mapped classes",
            context={"source": type(source).__name__, "target": type(target).__name__},
        )

    for column_attribute in inspect(type(target)).mapper.column_attrs:
        attribute_name = column_attribute.key
        if attribute_name in exclude:
            continue
        setattr(target, attribute_name, getattr(source, attribute_name))


class EntityStore[ModelT: Base]:
    """Reusable persistence mechanics for a single mapped class.

    Held by repositories through composition. It knows how to talk to a
    :class:`~sqlalchemy.orm.Session` for one model; it knows nothing about the
    domain meaning of that model, which is what makes it reusable across all
    seven of them.
    """

    def __init__(self, session: Session, model_type: type[ModelT]) -> None:
        self._session = session
        self._model_type = model_type

    @property
    def session(self) -> Session:
        """The session this store writes through.

        Exposed so a repository can build a statement that spans joins this
        store does not model, without opening a second session.
        """
        return self._session

    @property
    def model_type(self) -> type[ModelT]:
        """The mapped class this store persists."""
        return self._model_type

    def add(self, entity: ModelT) -> ModelT:
        """Stage ``entity`` for insertion and flush it.

        Flushing assigns the primary key immediately, which callers need in
        order to set foreign keys on children within the same transaction. It
        does not commit.

        Args:
            entity: Transient instance to persist.

        Returns:
            The same instance, now carrying its primary key.

        Raises:
            RepositoryError: If the insert violates a constraint.
        """
        with translate_database_errors("add", model=self._model_type.__name__):
            self._session.add(entity)
            self._session.flush()
        return entity

    def add_all(self, entities: Iterable[ModelT]) -> list[ModelT]:
        """Stage several entities for insertion and flush once.

        Args:
            entities: Transient instances to persist.

        Returns:
            The persisted instances, carrying primary keys.

        Raises:
            RepositoryError: If any insert violates a constraint.
        """
        materialized = list(entities)
        if not materialized:
            return []
        with translate_database_errors(
            "add_all",
            model=self._model_type.__name__,
            count=len(materialized),
        ):
            self._session.add_all(materialized)
            self._session.flush()
        return materialized

    def get_by_id(self, entity_id: int) -> ModelT | None:
        """Look up one row by primary key.

        Args:
            entity_id: Local primary key.

        Returns:
            The instance, or ``None`` when no row has that key.

        Raises:
            RepositoryError: If the query fails.
        """
        with translate_database_errors(
            "get_by_id",
            model=self._model_type.__name__,
            entity_id=entity_id,
        ):
            return self._session.get(self._model_type, entity_id)

    def require_by_id(self, entity_id: int) -> ModelT:
        """Look up one row by primary key, treating absence as an error.

        Args:
            entity_id: Local primary key.

        Returns:
            The instance.

        Raises:
            EntityNotFoundError: If no row has that key.
            RepositoryError: If the query fails.
        """
        entity = self.get_by_id(entity_id)
        if entity is None:
            raise EntityNotFoundError(
                f"No {self._model_type.__name__} with that identifier",
                context={"model": self._model_type.__name__, "entity_id": entity_id},
            )
        return entity

    def find_one(self, statement: Select[Any]) -> ModelT | None:
        """Execute ``statement`` and return its first mapped result.

        Args:
            statement: A select against this store's mapped class.

        Returns:
            The first result, or ``None`` when there are none.

        Raises:
            RepositoryError: If the query fails.
        """
        with translate_database_errors("find_one", model=self._model_type.__name__):
            result: ModelT | None = self._session.execute(statement).scalars().first()
        return result

    def find_all(self, statement: Select[Any]) -> list[ModelT]:
        """Execute ``statement`` and return all mapped results.

        Args:
            statement: A select against this store's mapped class.

        Returns:
            Every matching instance, in statement order.

        Raises:
            RepositoryError: If the query fails.
        """
        with translate_database_errors("find_all", model=self._model_type.__name__):
            results: list[ModelT] = list(self._session.execute(statement).scalars().all())
        return results

    def count(self) -> int:
        """Count every row of this store's mapped class.

        Returns:
            The row count.

        Raises:
            RepositoryError: If the query fails.
        """
        statement = select(func.count()).select_from(self._model_type)
        return self.scalar_count(statement)

    def scalar_count(self, statement: Select[Any]) -> int:
        """Execute a count statement and return the single integer result.

        Exists so that a filtered count is answered by the database rather than
        by loading rows and measuring the list, which is the difference between
        transferring one integer and transferring an account's entire campaign
        history.

        Args:
            statement: A select producing exactly one integer, typically
                ``select(func.count()).select_from(...).where(...)``.

        Returns:
            The counted value.

        Raises:
            RepositoryError: If the query fails.
        """
        with translate_database_errors("scalar_count", model=self._model_type.__name__):
            return int(self._session.execute(statement).scalar_one())

    def delete(self, entity: ModelT) -> None:
        """Delete one persistent instance and flush.

        Args:
            entity: The instance to delete.

        Raises:
            RepositoryError: If the delete violates a constraint.
        """
        with translate_database_errors("delete", model=self._model_type.__name__):
            self._session.delete(entity)
            self._session.flush()

    def flush(self) -> None:
        """Flush pending changes so generated values become visible.

        Raises:
            RepositoryError: If the flush violates a constraint.
        """
        with translate_database_errors("flush", model=self._model_type.__name__):
            self._session.flush()
