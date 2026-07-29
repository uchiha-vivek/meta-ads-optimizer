"""Declarative base, metadata conventions, and shared column mixins.

Every ORM model inherits from :class:`Base`, which carries a metadata object
with an explicit constraint naming convention. This matters more than it looks:
without it, PostgreSQL auto-generates constraint names, Alembic emits migrations
referring to constraints it cannot name, and a later ``DROP CONSTRAINT`` has
nothing to target. Setting the convention once, before the first migration, is
the only cheap moment to do it.
"""

from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal
from typing import Final

from sqlalchemy import DateTime, MetaData, Numeric, func
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Shared column dimensions. Declared once so that seven model modules cannot
# drift apart on how wide a name is or how much precision money carries.
NAME_MAX_LENGTH: Final[int] = 255
SHORT_TEXT_MAX_LENGTH: Final[int] = 64
CURRENCY_CODE_LENGTH: Final[int] = 3
URL_MAX_LENGTH: Final[int] = 2048

# Monetary amounts are stored as exact decimals, never as floats: binary
# floating point cannot represent 0.1, and summing thousands of ad spend rows
# in float silently accumulates error into figures a client reads as authority.
MONEY_PRECISION: Final[int] = 18
MONEY_SCALE: Final[int] = 6

# ix  index                %(column_0_label)s expands to table_column
# uq  unique constraint
# ck  check constraint     used by non-native enums
# fk  foreign key
# pk  primary key
_NAMING_CONVENTION: Final[dict[str, str]] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# Widest enum value the application stores, with room to grow. Non-native enums
# are VARCHAR columns and need an explicit length.
_ENUM_VALUE_MAX_LENGTH: Final[int] = 32


class Base(DeclarativeBase):
    """Declarative base shared by every ORM model in the application."""

    metadata = MetaData(naming_convention=_NAMING_CONVENTION)


def enum_column_type(enum_class: type[enum.Enum], *, name: str) -> SqlEnum:
    """Build a portable column type for a Python enum.

    Uses ``native_enum=False``, so the column is a ``VARCHAR`` guarded by a
    ``CHECK`` constraint rather than a PostgreSQL ``ENUM`` type. Native enum
    types require ``ALTER TYPE`` to add a member, which cannot run inside a
    transaction in older PostgreSQL and makes every value addition a bespoke
    migration. A check constraint is rewritten by ordinary DDL.

    ``values_callable`` stores ``member.value`` rather than ``member.name``, so
    the database holds ``"active"`` rather than ``"ACTIVE"`` and rows stay
    readable in ``psql``.

    Args:
        enum_class: The Python enum whose members are the permitted values.
        name: Name given to the generated check constraint.

    Returns:
        A configured SQLAlchemy ``Enum`` type.
    """
    return SqlEnum(
        enum_class,
        name=name,
        native_enum=False,
        length=_ENUM_VALUE_MAX_LENGTH,
        values_callable=lambda members: [str(member.value) for member in members],
        validate_strings=True,
    )


def money_column_type() -> Numeric[Decimal]:
    """Build the exact-decimal column type used for every monetary amount.

    Returns:
        A ``Numeric`` type with the shared precision and scale, mapping to
        :class:`decimal.Decimal` in Python rather than to ``float``.
    """
    return Numeric(MONEY_PRECISION, MONEY_SCALE, asdecimal=True)


class TimestampMixin:
    """Adds server-managed ``created_at`` and ``updated_at`` columns.

    Both defaults are evaluated by PostgreSQL rather than by Python, so rows
    written by a migration or by hand in ``psql`` are stamped identically to
    rows written through the ORM.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class RemoteObjectMixin:
    """Adds the ``remote_id`` column identifying an object in Meta's systems.

    Local integer primary keys are used for foreign keys because Meta's IDs are
    64-bit numbers delivered as strings and are not ours to trust as keys. The
    remote ID is kept unique and indexed because every synchronization looks
    rows up by it.
    """

    remote_id: Mapped[str] = mapped_column(
        nullable=False,
        unique=True,
        index=True,
        doc="Identifier assigned by Meta, e.g. a campaign or ad set ID.",
    )
