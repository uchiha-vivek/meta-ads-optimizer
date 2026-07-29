"""ORM model for a Meta ad set."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import (
    NAME_MAX_LENGTH,
    SHORT_TEXT_MAX_LENGTH,
    Base,
    RemoteObjectMixin,
    TimestampMixin,
    enum_column_type,
    money_column_type,
)
from app.models.enums import EntityStatus

if TYPE_CHECKING:
    from app.models.ad import Ad
    from app.models.campaign import Campaign


class AdSet(RemoteObjectMixin, TimestampMixin, Base):
    """An ad set: the budgeting, scheduling, and targeting unit of a campaign.

    This is where optimization usually acts. Unless the parent campaign uses
    Campaign Budget Optimization, the ad set holds the budget, the bid, and the
    optimization goal, which makes it the level at which spend can actually be
    shifted between audiences.

    Attributes:
        optimization_goal: What Meta's delivery system optimizes toward, e.g.
            ``OFFSITE_CONVERSIONS``. Two ad sets with different goals are not
            comparable on cost per result, because the result is not the same
            event.
        billing_event: What the advertiser is charged for, e.g. ``IMPRESSIONS``.
        bid_amount: Bid cap or target cost in major currency units, depending on
            the parent campaign's bid strategy.
    """

    __tablename__ = "ad_sets"

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str | None] = mapped_column(String(NAME_MAX_LENGTH))
    status: Mapped[EntityStatus] = mapped_column(
        enum_column_type(EntityStatus, name="ad_set_status"),
        nullable=False,
        default=EntityStatus.UNKNOWN,
    )
    effective_status: Mapped[str | None] = mapped_column(String(SHORT_TEXT_MAX_LENGTH))
    optimization_goal: Mapped[str | None] = mapped_column(String(SHORT_TEXT_MAX_LENGTH))
    billing_event: Mapped[str | None] = mapped_column(String(SHORT_TEXT_MAX_LENGTH))
    daily_budget: Mapped[Decimal | None] = mapped_column(money_column_type())
    lifetime_budget: Mapped[Decimal | None] = mapped_column(money_column_type())
    bid_amount: Mapped[Decimal | None] = mapped_column(money_column_type())
    start_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    campaign: Mapped[Campaign] = relationship(back_populates="ad_sets")
    ads: Mapped[list[Ad]] = relationship(
        back_populates="ad_set",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    @property
    def effective_daily_budget(self) -> Decimal | None:
        """The daily budget in force at this level, if one is set here."""
        return self.daily_budget

    @property
    def is_delivering(self) -> bool:
        """Whether the advertiser has this ad set configured to spend."""
        return self.status.is_delivering

    def __repr__(self) -> str:
        """Return an unambiguous representation for logs and debugging."""
        return (
            f"AdSet(id={self.id!r}, remote_id={self.remote_id!r}, "
            f"name={self.name!r}, status={self.status.value!r})"
        )
