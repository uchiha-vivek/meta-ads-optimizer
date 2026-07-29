"""Persistence for :class:`~app.models.insight.InsightRecord`."""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.enums import InsightLevel
from app.models.insight import InsightRecord
from app.repositories.base import EntityStore, copy_scalar_columns, translate_database_errors


class InsightRepository:
    """Reads and writes performance insight rows.

    Identity here is the reporting window, not a Meta ID: a row is uniquely
    ``(level, entity, date_start, date_stop)``. Re-fetching a window must update
    the row already covering it, because Meta restates recent days as
    attribution windows close, and inserting instead would double every
    aggregate computed over that period.
    """

    def __init__(self, session: Session) -> None:
        self._store: EntityStore[InsightRecord] = EntityStore(session, InsightRecord)

    def get_for_window(
        self,
        *,
        level: InsightLevel,
        entity_remote_id: str,
        date_start: date,
        date_stop: date,
    ) -> InsightRecord | None:
        """Find the row covering exactly one reporting window.

        Args:
            level: Aggregation level of the row.
            entity_remote_id: Meta ID of the measured entity.
            date_start: First day of the window, inclusive.
            date_stop: Last day of the window, inclusive.

        Returns:
            The row, or ``None`` when that window has not been fetched.
        """
        statement = select(InsightRecord).where(
            InsightRecord.level == level,
            InsightRecord.entity_remote_id == entity_remote_id,
            InsightRecord.date_start == date_start,
            InsightRecord.date_stop == date_stop,
        )
        return self._store.find_one(statement)

    def list_for_entity(
        self,
        *,
        level: InsightLevel,
        entity_remote_id: str,
        since: date,
        until: date,
    ) -> list[InsightRecord]:
        """List an entity's rows across a date range, oldest first.

        Args:
            level: Aggregation level to read.
            entity_remote_id: Meta ID of the measured entity.
            since: Earliest ``date_start`` to include, inclusive.
            until: Latest ``date_stop`` to include, inclusive.

        Returns:
            Matching rows in chronological order, which is the order every
            trend calculation expects.
        """
        statement = (
            select(InsightRecord)
            .where(
                InsightRecord.level == level,
                InsightRecord.entity_remote_id == entity_remote_id,
                InsightRecord.date_start >= since,
                InsightRecord.date_stop <= until,
            )
            .order_by(InsightRecord.date_start)
        )
        return self._store.find_all(statement)

    def list_for_account(
        self,
        ad_account_id: int,
        *,
        level: InsightLevel,
        since: date,
        until: date,
    ) -> list[InsightRecord]:
        """List every row for an account at one level across a date range.

        This is the bulk read that feeds the recommendation engine: one query
        returns the whole account's history, which is then grouped in memory,
        rather than issuing a query per campaign.

        Args:
            ad_account_id: Local primary key of the owning account.
            level: Aggregation level to read.
            since: Earliest ``date_start`` to include, inclusive.
            until: Latest ``date_stop`` to include, inclusive.

        Returns:
            Matching rows ordered by entity, then chronologically.
        """
        statement = (
            select(InsightRecord)
            .where(
                InsightRecord.ad_account_id == ad_account_id,
                InsightRecord.level == level,
                InsightRecord.date_start >= since,
                InsightRecord.date_stop <= until,
            )
            .order_by(InsightRecord.entity_remote_id, InsightRecord.date_start)
        )
        return self._store.find_all(statement)

    def latest_date_for_account(self, ad_account_id: int, *, level: InsightLevel) -> date | None:
        """Return the most recent day covered for an account at one level.

        Lets a sync fetch only the days it is missing instead of re-downloading
        the full window on every run.

        Args:
            ad_account_id: Local primary key of the owning account.
            level: Aggregation level to inspect.

        Returns:
            The latest ``date_stop``, or ``None`` when nothing is stored.
        """
        statement = (
            select(func.max(InsightRecord.date_stop))
            .select_from(InsightRecord)
            .where(
                InsightRecord.ad_account_id == ad_account_id,
                InsightRecord.level == level,
            )
        )
        with translate_database_errors(
            "latest_date_for_account",
            ad_account_id=ad_account_id,
            level=level.value,
        ):
            latest = self._store.session.execute(statement).scalar_one_or_none()
        # `max()` over an empty set yields NULL, and the aggregate is untyped at
        # the ORM boundary, so the result is narrowed explicitly.
        return latest if isinstance(latest, date) else None

    def upsert(self, incoming: InsightRecord) -> InsightRecord:
        """Insert ``incoming``, or refresh the row covering the same window.

        Args:
            incoming: Transient row built from a Graph API insights response.

        Returns:
            The persistent instance, whether newly inserted or updated.
        """
        existing = self.get_for_window(
            level=incoming.level,
            entity_remote_id=incoming.entity_remote_id,
            date_start=incoming.date_start,
            date_stop=incoming.date_stop,
        )
        if existing is None:
            return self._store.add(incoming)
        copy_scalar_columns(source=incoming, target=existing)
        self._store.flush()
        return existing

    def count_for_account(self, ad_account_id: int) -> int:
        """Count the stored rows for one account.

        Args:
            ad_account_id: Local primary key of the owning account.

        Returns:
            The number of stored insight rows.
        """
        statement = (
            select(func.count())
            .select_from(InsightRecord)
            .where(InsightRecord.ad_account_id == ad_account_id)
        )
        return self._store.scalar_count(statement)
