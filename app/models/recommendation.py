"""ORM model for a stored optimization recommendation."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import (
    NAME_MAX_LENGTH,
    SHORT_TEXT_MAX_LENGTH,
    Base,
    TimestampMixin,
    enum_column_type,
)
from app.models.enums import (
    InsightLevel,
    RecommendationAction,
    RecommendationSeverity,
    RecommendationStatus,
)

if TYPE_CHECKING:
    from app.models.ad_account import AdAccount


class Recommendation(TimestampMixin, Base):
    """A proposed change to a campaign, ad set, or ad, with its justification.

    Recommendations are persisted rather than merely printed for three reasons.
    An advertiser needs to see whether last week's advice was acted on; applying
    a change requires a record of what was applied, to what, and when, if it is
    ever to be undone; and the engine itself must not re-raise advice a human
    already dismissed.

    :attr:`metric_snapshot` captures the figures that triggered the rule at the
    moment it fired. Without it, a recommendation read three days later cannot
    be evaluated at all, because the metrics it was based on have since moved.

    :attr:`suggested_change` describes the proposed mutation in machine-readable
    form — the field, its current value, and its proposed value — which is what
    lets ``meta optimize --apply`` execute the change rather than re-derive it
    from prose.

    Attributes:
        rule_code: Stable identifier of the rule that fired, e.g.
            ``creative_fatigue``. Used for deduplication and for suppressing
            rules an advertiser does not want.
        entity_remote_id: Meta ID of the entity the advice concerns. Carries no
            foreign key so that history outlives the entity, matching
            :class:`~app.models.insight.InsightRecord`.
    """

    __tablename__ = "recommendations"
    __table_args__ = (
        # Supports the two dominant reads: everything open for an account, and
        # the deduplication check for one rule against one entity.
        Index("ix_recommendations_account_status", "ad_account_id", "status"),
        Index("ix_recommendations_entity_rule", "entity_remote_id", "rule_code"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ad_account_id: Mapped[int] = mapped_column(
        ForeignKey("ad_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    level: Mapped[InsightLevel] = mapped_column(
        enum_column_type(InsightLevel, name="recommendation_level"),
        nullable=False,
    )
    entity_remote_id: Mapped[str] = mapped_column(nullable=False)
    entity_name: Mapped[str | None] = mapped_column(String(NAME_MAX_LENGTH))

    rule_code: Mapped[str] = mapped_column(String(SHORT_TEXT_MAX_LENGTH), nullable=False)
    severity: Mapped[RecommendationSeverity] = mapped_column(
        enum_column_type(RecommendationSeverity, name="recommendation_severity"),
        nullable=False,
    )
    action: Mapped[RecommendationAction] = mapped_column(
        enum_column_type(RecommendationAction, name="recommendation_action"),
        nullable=False,
    )
    status: Mapped[RecommendationStatus] = mapped_column(
        enum_column_type(RecommendationStatus, name="recommendation_status"),
        nullable=False,
        default=RecommendationStatus.OPEN,
    )

    title: Mapped[str] = mapped_column(String(NAME_MAX_LENGTH), nullable=False)
    rationale: Mapped[str] = mapped_column(Text(), nullable=False)

    metric_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON(), nullable=False, default=dict)
    suggested_change: Mapped[dict[str, Any]] = mapped_column(JSON(), nullable=False, default=dict)

    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    ad_account: Mapped[AdAccount] = relationship(back_populates="recommendations")

    @property
    def is_open(self) -> bool:
        """Whether this recommendation still awaits a decision."""
        return self.status is RecommendationStatus.OPEN

    def __repr__(self) -> str:
        """Return an unambiguous representation for logs and debugging."""
        return (
            f"Recommendation(id={self.id!r}, rule_code={self.rule_code!r}, "
            f"entity_remote_id={self.entity_remote_id!r}, "
            f"severity={self.severity.value!r}, status={self.status.value!r})"
        )
