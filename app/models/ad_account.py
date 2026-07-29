"""ORM model for a Meta ad account."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import (
    CURRENCY_CODE_LENGTH,
    NAME_MAX_LENGTH,
    SHORT_TEXT_MAX_LENGTH,
    Base,
    RemoteObjectMixin,
    TimestampMixin,
    money_column_type,
)

if TYPE_CHECKING:
    from app.models.ad_creative import AdCreative
    from app.models.campaign import Campaign
    from app.models.insight import InsightRecord
    from app.models.recommendation import Recommendation


class AdAccount(RemoteObjectMixin, TimestampMixin, Base):
    """A Meta advertising account and the root of every owned object.

    The account owns the currency and timezone that give every stored figure its
    meaning. A spend of ``1000`` is not comparable across accounts without
    knowing the currency, and a daily insights row is only unambiguous relative
    to the account's timezone, because Meta cuts its reporting days there rather
    than in UTC.

    Attributes:
        remote_id: Meta's account ID, always prefixed ``act_``.
        currency: ISO 4217 code every monetary amount on owned rows is expressed
            in.
        timezone_name: IANA timezone Meta uses to delimit reporting days.
        account_status: Meta's numeric account state; 1 means active.
    """

    __tablename__ = "ad_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str | None] = mapped_column(String(NAME_MAX_LENGTH))
    business_name: Mapped[str | None] = mapped_column(String(NAME_MAX_LENGTH))
    currency: Mapped[str | None] = mapped_column(String(CURRENCY_CODE_LENGTH))
    timezone_name: Mapped[str | None] = mapped_column(String(SHORT_TEXT_MAX_LENGTH))
    account_status: Mapped[int | None] = mapped_column()
    spend_cap: Mapped[Decimal | None] = mapped_column(money_column_type())
    amount_spent: Mapped[Decimal | None] = mapped_column(money_column_type())

    campaigns: Mapped[list[Campaign]] = relationship(
        back_populates="ad_account",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    creatives: Mapped[list[AdCreative]] = relationship(
        back_populates="ad_account",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    insights: Mapped[list[InsightRecord]] = relationship(
        back_populates="ad_account",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    recommendations: Mapped[list[Recommendation]] = relationship(
        back_populates="ad_account",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        """Return an unambiguous representation for logs and debugging."""
        return f"AdAccount(id={self.id!r}, remote_id={self.remote_id!r}, name={self.name!r})"
