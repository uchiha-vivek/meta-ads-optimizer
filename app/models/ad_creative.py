"""ORM model for a Meta ad creative."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import (
    NAME_MAX_LENGTH,
    SHORT_TEXT_MAX_LENGTH,
    URL_MAX_LENGTH,
    Base,
    RemoteObjectMixin,
    TimestampMixin,
)

if TYPE_CHECKING:
    from app.models.ad import Ad
    from app.models.ad_account import AdAccount


class AdCreative(RemoteObjectMixin, TimestampMixin, Base):
    """The rendered content of an ad: copy, imagery, and call to action.

    Owned by the account rather than by an ad, mirroring Meta's model, in which
    a creative lives in the account's creative library and is referenced by any
    number of ads.

    Body text is stored as unbounded ``TEXT``. Meta imposes no length its API
    documents reliably, and truncating an advertiser's copy to fit a column
    would corrupt the record of what actually ran.
    """

    __tablename__ = "ad_creatives"

    id: Mapped[int] = mapped_column(primary_key=True)
    ad_account_id: Mapped[int] = mapped_column(
        ForeignKey("ad_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str | None] = mapped_column(String(NAME_MAX_LENGTH))
    title: Mapped[str | None] = mapped_column(Text())
    body: Mapped[str | None] = mapped_column(Text())
    call_to_action_type: Mapped[str | None] = mapped_column(String(SHORT_TEXT_MAX_LENGTH))
    object_type: Mapped[str | None] = mapped_column(String(SHORT_TEXT_MAX_LENGTH))
    thumbnail_url: Mapped[str | None] = mapped_column(String(URL_MAX_LENGTH))
    image_url: Mapped[str | None] = mapped_column(String(URL_MAX_LENGTH))
    video_id: Mapped[str | None] = mapped_column(String(SHORT_TEXT_MAX_LENGTH))

    ad_account: Mapped[AdAccount] = relationship(back_populates="creatives")
    ads: Mapped[list[Ad]] = relationship(back_populates="creative")

    @property
    def is_video(self) -> bool:
        """Whether this creative is backed by a video asset."""
        return self.video_id is not None

    def __repr__(self) -> str:
        """Return an unambiguous representation for logs and debugging."""
        return f"AdCreative(id={self.id!r}, remote_id={self.remote_id!r}, name={self.name!r})"
