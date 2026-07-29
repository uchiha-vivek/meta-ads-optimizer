"""ORM model for a Meta campaign."""

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
    from app.models.ad_account import AdAccount
    from app.models.ad_set import AdSet


class Campaign(RemoteObjectMixin, TimestampMixin, Base):
    """A campaign: the objective-level container beneath an ad account.

    Budget may be held here or on the child ad sets, never usefully on both.
    A campaign carrying ``daily_budget`` uses Campaign Budget Optimization, and
    Meta distributes that budget across its ad sets automatically. The
    optimization service reads :attr:`uses_campaign_budget_optimization` to
    decide whether a budget recommendation targets this row or its children;
    writing a budget to the wrong level is silently ignored by the API.

    Monetary amounts are stored in major currency units (for example dollars).
    Meta transmits them as integer strings in minor units, and that conversion
    happens once, in the API schema layer, so nothing downstream has to remember
    whether a number means cents or dollars.

    Attributes:
        status: The status the advertiser configured.
        effective_status: The status Meta computes after accounting for parent
            state, billing, and review. Kept as free text because Meta extends
            this vocabulary without notice, and an unknown value must not fail a
            sync.
    """

    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(primary_key=True)
    ad_account_id: Mapped[int] = mapped_column(
        ForeignKey("ad_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str | None] = mapped_column(String(NAME_MAX_LENGTH))
    status: Mapped[EntityStatus] = mapped_column(
        enum_column_type(EntityStatus, name="campaign_status"),
        nullable=False,
        default=EntityStatus.UNKNOWN,
    )
    effective_status: Mapped[str | None] = mapped_column(String(SHORT_TEXT_MAX_LENGTH))
    objective: Mapped[str | None] = mapped_column(String(SHORT_TEXT_MAX_LENGTH))
    buying_type: Mapped[str | None] = mapped_column(String(SHORT_TEXT_MAX_LENGTH))
    bid_strategy: Mapped[str | None] = mapped_column(String(SHORT_TEXT_MAX_LENGTH))
    daily_budget: Mapped[Decimal | None] = mapped_column(money_column_type())
    lifetime_budget: Mapped[Decimal | None] = mapped_column(money_column_type())
    start_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stop_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    ad_account: Mapped[AdAccount] = relationship(back_populates="campaigns")
    ad_sets: Mapped[list[AdSet]] = relationship(
        back_populates="campaign",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    @property
    def uses_campaign_budget_optimization(self) -> bool:
        """Whether the budget lives on this campaign rather than its ad sets."""
        return self.daily_budget is not None or self.lifetime_budget is not None

    @property
    def is_delivering(self) -> bool:
        """Whether the advertiser has this campaign configured to spend."""
        return self.status.is_delivering

    def __repr__(self) -> str:
        """Return an unambiguous representation for logs and debugging."""
        return (
            f"Campaign(id={self.id!r}, remote_id={self.remote_id!r}, "
            f"name={self.name!r}, status={self.status.value!r})"
        )
