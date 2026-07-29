"""Persistence for :class:`~app.models.recommendation.Recommendation`."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.enums import RecommendationStatus
from app.models.recommendation import Recommendation
from app.repositories.base import EntityStore


class RecommendationRepository:
    """Reads and writes stored optimization recommendations.

    The engine is deterministic, so re-running it over unchanged data would
    re-derive the same advice. :meth:`supersede_open_for_rule` marks the
    previous generation superseded before the new one is written, so an
    advertiser sees the current state of each finding rather than one row per
    time the command happened to be run.
    """

    def __init__(self, session: Session) -> None:
        self._store: EntityStore[Recommendation] = EntityStore(session, Recommendation)

    def add(self, recommendation: Recommendation) -> Recommendation:
        """Persist one recommendation.

        Args:
            recommendation: Transient recommendation to store.

        Returns:
            The persistent instance, carrying its primary key.
        """
        return self._store.add(recommendation)

    def add_all(self, recommendations: Iterable[Recommendation]) -> list[Recommendation]:
        """Persist several recommendations in one flush.

        Args:
            recommendations: Transient recommendations to store.

        Returns:
            The persistent instances.
        """
        return self._store.add_all(recommendations)

    def require_by_id(self, recommendation_id: int) -> Recommendation:
        """Fetch one recommendation by primary key.

        Args:
            recommendation_id: Local primary key.

        Returns:
            The recommendation.

        Raises:
            EntityNotFoundError: If no recommendation has that key.
        """
        return self._store.require_by_id(recommendation_id)

    def list_open_for_account(self, ad_account_id: int) -> list[Recommendation]:
        """List an account's outstanding recommendations.

        Ordering is by generation time rather than severity: severity is an enum
        stored as text, so sorting it in SQL would order alphabetically, putting
        ``critical`` before ``info`` by luck rather than by meaning. Ranking by
        urgency happens in the service, using
        :attr:`~app.models.enums.RecommendationSeverity.rank`.

        Args:
            ad_account_id: Local primary key of the owning account.

        Returns:
            Open recommendations, most recently generated first.
        """
        statement = (
            select(Recommendation)
            .where(
                Recommendation.ad_account_id == ad_account_id,
                Recommendation.status == RecommendationStatus.OPEN,
            )
            .order_by(Recommendation.generated_at.desc(), Recommendation.id.desc())
        )
        return self._store.find_all(statement)

    def find_open_for_rule(
        self,
        *,
        entity_remote_id: str,
        rule_code: str,
    ) -> Recommendation | None:
        """Find the outstanding recommendation for one entity and rule.

        Args:
            entity_remote_id: Meta ID of the entity the advice concerns.
            rule_code: Stable identifier of the rule that produced it.

        Returns:
            The open recommendation, or ``None`` when there is none.
        """
        statement = (
            select(Recommendation)
            .where(
                Recommendation.entity_remote_id == entity_remote_id,
                Recommendation.rule_code == rule_code,
                Recommendation.status == RecommendationStatus.OPEN,
            )
            .order_by(Recommendation.generated_at.desc())
        )
        return self._store.find_one(statement)

    def supersede_open_for_rule(self, *, entity_remote_id: str, rule_code: str) -> int:
        """Mark every open recommendation for one entity and rule superseded.

        Args:
            entity_remote_id: Meta ID of the entity the advice concerns.
            rule_code: Stable identifier of the rule that produced it.

        Returns:
            How many rows were superseded.
        """
        statement = select(Recommendation).where(
            Recommendation.entity_remote_id == entity_remote_id,
            Recommendation.rule_code == rule_code,
            Recommendation.status == RecommendationStatus.OPEN,
        )
        superseded = self._store.find_all(statement)
        for recommendation in superseded:
            recommendation.status = RecommendationStatus.SUPERSEDED
        self._store.flush()
        return len(superseded)

    def mark_applied(self, recommendation: Recommendation, *, applied_at: datetime) -> None:
        """Record that a recommendation was carried out.

        Args:
            recommendation: The persistent recommendation to update.
            applied_at: Timezone-aware moment the change was applied.
        """
        recommendation.status = RecommendationStatus.APPLIED
        recommendation.applied_at = applied_at
        self._store.flush()

    def mark_dismissed(self, recommendation: Recommendation) -> None:
        """Record that an advertiser rejected a recommendation.

        Args:
            recommendation: The persistent recommendation to update.
        """
        recommendation.status = RecommendationStatus.DISMISSED
        self._store.flush()

    def count_open_for_account(self, ad_account_id: int) -> int:
        """Count an account's outstanding recommendations.

        Args:
            ad_account_id: Local primary key of the owning account.

        Returns:
            The number of open recommendations.
        """
        statement = (
            select(func.count())
            .select_from(Recommendation)
            .where(
                Recommendation.ad_account_id == ad_account_id,
                Recommendation.status == RecommendationStatus.OPEN,
            )
        )
        return self._store.scalar_count(statement)
