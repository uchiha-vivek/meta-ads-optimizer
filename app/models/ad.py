"""ORM model for a Meta ad."""

from __future__ import annotations

from datetime import datetime
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
)
from app.models.enums import EntityStatus

if TYPE_CHECKING:
    from app.models.ad_creative import AdCreative
    from app.models.ad_set import AdSet


class Ad(RemoteObjectMixin, TimestampMixin, Base):
    """An ad: the pairing of a creative with an ad set.

    Modelled explicitly rather than collapsed into the creative because the
    relationship is many-to-one in Meta's data model. One creative is commonly
    reused across several ad sets, and conflating the two would make it
    impossible to tell whether a creative underperforms everywhere or only in
    one audience — which is exactly the question the creative fatigue rule asks.

    The link to a creative is nullable: an ad can be synchronized before its
    creative has been fetched, and losing the ad row for that reason would be
    worse than storing it with the reference unresolved.
    """

    __tablename__ = "ads"

    id: Mapped[int] = mapped_column(primary_key=True)
    ad_set_id: Mapped[int] = mapped_column(
        ForeignKey("ad_sets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    creative_id: Mapped[int | None] = mapped_column(
        ForeignKey("ad_creatives.id", ondelete="SET NULL"),
        index=True,
    )
    name: Mapped[str | None] = mapped_column(String(NAME_MAX_LENGTH))
    status: Mapped[EntityStatus] = mapped_column(
        enum_column_type(EntityStatus, name="ad_status"),
        nullable=False,
        default=EntityStatus.UNKNOWN,
    )
    effective_status: Mapped[str | None] = mapped_column(String(SHORT_TEXT_MAX_LENGTH))
    created_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    ad_set: Mapped[AdSet] = relationship(back_populates="ads")
    creative: Mapped[AdCreative | None] = relationship(back_populates="ads")

    @property
    def is_delivering(self) -> bool:
        """Whether the advertiser has this ad configured to spend."""
        return self.status.is_delivering

    def __repr__(self) -> str:
        """Return an unambiguous representation for logs and debugging."""
        return (
            f"Ad(id={self.id!r}, remote_id={self.remote_id!r}, "
            f"name={self.name!r}, status={self.status.value!r})"
        )
