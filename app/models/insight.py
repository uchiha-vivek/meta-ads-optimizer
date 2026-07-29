"""ORM model for a performance insights row."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, ForeignKey, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import (
    Base,
    TimestampMixin,
    enum_column_type,
    money_column_type,
)
from app.models.enums import InsightLevel

if TYPE_CHECKING:
    from app.models.ad_account import AdAccount


class InsightRecord(TimestampMixin, Base):
    """Measured performance for one entity over one reporting window.

    A single table serves all four aggregation levels, discriminated by
    :attr:`level` and :attr:`entity_remote_id`, mirroring the Graph API's own
    ``level`` parameter. Four near-identical tables would need four repositories
    and four copies of every trend query, all to express the same tuple of
    metrics.

    This is the reason the project has a database at all. Meta's insights
    endpoint answers "what is happening now"; it cannot answer "is cost per
    result worse than last week", because comparing windows requires having kept
    the earlier one. Every recommendation that reasons about a trend reads from
    this table.

    **Only measured quantities are stored.** CTR, CPC, CPM, CPA, ROAS, and
    frequency are all deterministic functions of these columns and are computed
    in :mod:`app.analytics.metrics`. Storing them too would mean two definitions
    of the same number, free to disagree after any partial backfill.

    Note that :attr:`entity_remote_id` intentionally carries no foreign key. An
    insights row must remain valid after its campaign is deleted from Meta;
    losing the spend history of a deleted campaign would destroy exactly the
    record needed to explain last month's results.

    Attributes:
        date_start: First day of the reporting window, inclusive, in the
            account's timezone.
        date_stop: Last day of the reporting window, inclusive.
        conversions: Count of the optimized conversion event, typically
            purchases.
        conversion_value: Revenue attributed to those conversions, in major
            currency units.
    """

    __tablename__ = "insight_records"
    __table_args__ = (
        # One row per entity per window. Re-syncing a window must update the
        # existing row rather than accumulate duplicates that would double every
        # subsequent aggregate.
        UniqueConstraint(
            "level",
            "entity_remote_id",
            "date_start",
            "date_stop",
            name="entity_reporting_window",
        ),
        # Supports the dominant read pattern: one account, one level, ordered
        # by date, which is what every trend comparison issues.
        Index("ix_insight_records_account_level_date", "ad_account_id", "level", "date_start"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ad_account_id: Mapped[int] = mapped_column(
        ForeignKey("ad_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    level: Mapped[InsightLevel] = mapped_column(
        enum_column_type(InsightLevel, name="insight_level"),
        nullable=False,
    )
    entity_remote_id: Mapped[str] = mapped_column(nullable=False, index=True)
    entity_name: Mapped[str | None] = mapped_column()

    date_start: Mapped[date] = mapped_column(nullable=False)
    date_stop: Mapped[date] = mapped_column(nullable=False)

    spend: Mapped[Decimal] = mapped_column(money_column_type(), nullable=False, default=Decimal(0))
    impressions: Mapped[int] = mapped_column(BigInteger(), nullable=False, default=0)
    clicks: Mapped[int] = mapped_column(BigInteger(), nullable=False, default=0)
    reach: Mapped[int] = mapped_column(BigInteger(), nullable=False, default=0)
    conversions: Mapped[int] = mapped_column(BigInteger(), nullable=False, default=0)
    conversion_value: Mapped[Decimal] = mapped_column(
        money_column_type(),
        nullable=False,
        default=Decimal(0),
    )

    ad_account: Mapped[AdAccount] = relationship(back_populates="insights")

    @property
    def day_count(self) -> int:
        """Number of days the reporting window covers, inclusive of both ends."""
        return (self.date_stop - self.date_start).days + 1

    def __repr__(self) -> str:
        """Return an unambiguous representation for logs and debugging."""
        return (
            f"InsightRecord(level={self.level.value!r}, "
            f"entity_remote_id={self.entity_remote_id!r}, "
            f"date_start={self.date_start!r}, spend={self.spend!r})"
        )
