"""Business logic for generating and applying optimization recommendations.

Generation is read-only with respect to Meta: it reads stored history, runs the
rule engine, and persists findings. Application is the only place in the entire
project that changes an advertiser's live account, which is why it is a separate
method, requires an explicit request per recommendation, and refuses anything
the engine did not mark automatable.

Application deliberately performs the API call *outside* the database
transaction. Holding a transaction open across a network call would keep row
locks for the duration of an unpredictable round trip, and a transaction that
rolled back after the call had already succeeded would leave the database
claiming a change was never applied when Meta had in fact applied it. The order
here — read, call, then record — means the worst case is a change that succeeded
remotely but was not recorded locally, which the next sync repairs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from app.api.client import MetaMarketingClient
from app.models.enums import InsightLevel, RecommendationAction
from app.models.recommendation import Recommendation
from app.recommendations.context import RecommendationContext, RecommendationProposal
from app.recommendations.engine import RecommendationEngine
from app.repositories.unit_of_work import UnitOfWork, UnitOfWorkFactory
from app.services.insight_service import InsightService
from app.utils.exceptions import OptimizationError
from app.utils.money import major_units_to_minor

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EntityAttributes:
    """The configuration facts a rule needs beyond raw metrics.

    Attributes:
        daily_budget: Budget in force at this entity's level, in major currency
            units, or ``None`` when the budget is held elsewhere.
        is_delivering: Whether the advertiser has this entity set to spend.
    """

    daily_budget: Decimal | None
    is_delivering: bool


@dataclass(frozen=True, slots=True)
class OptimizationResult:
    """What one generation run produced.

    Attributes:
        account_remote_id: Meta ID of the account evaluated.
        currency: ISO 4217 code the monetary figures are expressed in.
        level: Aggregation level evaluated.
        recommendations: Findings, most urgent first.
        entities_evaluated: How many entities were examined, which is the
            denominator that makes an empty result meaningful — no findings
            across two hundred campaigns says something quite different from no
            findings across none.
    """

    account_remote_id: str
    currency: str | None
    level: InsightLevel
    recommendations: list[Recommendation]
    entities_evaluated: int


class OptimizationService:
    """Generates recommendations and, on request, applies them."""

    def __init__(
        self,
        *,
        unit_of_work_factory: UnitOfWorkFactory,
        insight_service: InsightService,
        engine: RecommendationEngine,
        client: MetaMarketingClient,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._insight_service = insight_service
        self._engine = engine
        self._client = client

    def generate_recommendations(
        self,
        account_remote_id: str,
        *,
        level: InsightLevel,
        since: date,
        until: date,
        refresh: bool = False,
    ) -> OptimizationResult:
        """Evaluate an account and persist the resulting findings.

        Previously open findings for the same entity and rule are superseded
        before the new ones are written, so the stored set always reflects the
        current state rather than accumulating one row per time the command was
        run.

        Args:
            account_remote_id: Meta account ID, including the ``act_`` prefix.
            level: Aggregation level to evaluate.
            since: First day of the window, inclusive.
            until: Last day of the window, inclusive.
            refresh: Fetch fresh insights from Meta before evaluating.

        Returns:
            The findings and the count of entities examined.

        Raises:
            EntityNotFoundError: If the account has not been synchronized.
            MetaApiError: If ``refresh`` is set and a Graph API request fails.
        """
        report = self._insight_service.performance_report(
            account_remote_id,
            level=level,
            since=since,
            until=until,
            refresh=refresh,
        )

        with self._unit_of_work_factory.start() as unit_of_work:
            account = unit_of_work.ad_accounts.require_by_remote_id(account_remote_id)
            attributes = _load_entity_attributes(unit_of_work, account.id, level)

            contexts = [
                RecommendationContext(
                    level=level,
                    entity_remote_id=entry.entity_remote_id,
                    entity_name=entry.entity_name,
                    currency=report.currency,
                    current=entry.current,
                    comparison=entry.comparison,
                    daily_budget=_attribute_for(attributes, entry.entity_remote_id).daily_budget,
                    is_delivering=_attribute_for(attributes, entry.entity_remote_id).is_delivering,
                )
                for entry in report.entries
            ]

            findings = self._engine.evaluate_all(contexts)
            generated_at = datetime.now(UTC)
            stored: list[Recommendation] = []

            for context, proposal in findings:
                unit_of_work.recommendations.supersede_open_for_rule(
                    entity_remote_id=context.entity_remote_id,
                    rule_code=proposal.rule_code,
                )
                stored.append(
                    unit_of_work.recommendations.add(
                        _to_recommendation_model(
                            context,
                            proposal,
                            ad_account_id=account.id,
                            generated_at=generated_at,
                        )
                    )
                )

        _logger.info(
            "Generated recommendations",
            extra={
                "account_remote_id": account_remote_id,
                "level": level.value,
                "entities_evaluated": len(contexts),
                "recommendations": len(stored),
            },
        )
        return OptimizationResult(
            account_remote_id=account_remote_id,
            currency=report.currency,
            level=level,
            recommendations=stored,
            entities_evaluated=len(contexts),
        )

    def list_open_recommendations(self, account_remote_id: str) -> list[Recommendation]:
        """List an account's outstanding recommendations, most urgent first.

        Args:
            account_remote_id: Meta account ID, including the ``act_`` prefix.

        Returns:
            Open recommendations sorted by severity, then recency.

        Raises:
            EntityNotFoundError: If the account has not been synchronized.
        """
        with self._unit_of_work_factory.start() as unit_of_work:
            account = unit_of_work.ad_accounts.require_by_remote_id(account_remote_id)
            open_recommendations = unit_of_work.recommendations.list_open_for_account(account.id)

        return sorted(
            open_recommendations,
            key=lambda recommendation: (recommendation.severity.rank, recommendation.generated_at),
            reverse=True,
        )

    def apply_recommendation(self, recommendation_id: int) -> Recommendation:
        """Carry out one recommendation against the live Meta account.

        Args:
            recommendation_id: Local primary key of the recommendation.

        Returns:
            The recommendation, now marked applied.

        Raises:
            EntityNotFoundError: If no recommendation has that key.
            OptimizationError: If the recommendation is not automatable, has
                already been resolved, or describes a change this service does
                not know how to perform.
            MetaApiError: If the Graph API rejects the change.
        """
        with self._unit_of_work_factory.start() as unit_of_work:
            recommendation = unit_of_work.recommendations.require_by_id(recommendation_id)
            if not recommendation.is_open:
                raise OptimizationError(
                    "Recommendation is no longer open and cannot be applied",
                    context={
                        "recommendation_id": recommendation_id,
                        "status": recommendation.status.value,
                    },
                )
            account = unit_of_work.ad_accounts.require_by_id(recommendation.ad_account_id)
            plan = _ApplicationPlan(
                entity_remote_id=recommendation.entity_remote_id,
                action=recommendation.action,
                suggested_change=dict(recommendation.suggested_change),
                currency=account.currency,
            )

        # Outside the transaction: see the module docstring on ordering.
        self._execute(plan)

        with self._unit_of_work_factory.start() as unit_of_work:
            applied = unit_of_work.recommendations.require_by_id(recommendation_id)
            unit_of_work.recommendations.mark_applied(applied, applied_at=datetime.now(UTC))

        _logger.info(
            "Applied recommendation",
            extra={
                "recommendation_id": recommendation_id,
                "action": plan.action.value,
                "entity_remote_id": plan.entity_remote_id,
            },
        )
        return applied

    def dismiss_recommendation(self, recommendation_id: int) -> Recommendation:
        """Record that an advertiser rejected a recommendation.

        Args:
            recommendation_id: Local primary key of the recommendation.

        Returns:
            The recommendation, now marked dismissed.

        Raises:
            EntityNotFoundError: If no recommendation has that key.
        """
        with self._unit_of_work_factory.start() as unit_of_work:
            recommendation = unit_of_work.recommendations.require_by_id(recommendation_id)
            unit_of_work.recommendations.mark_dismissed(recommendation)
        return recommendation

    def _execute(self, plan: _ApplicationPlan) -> None:
        """Perform the API call one recommendation describes."""
        if plan.action is RecommendationAction.PAUSE_ENTITY:
            self._client.pause_entity(plan.entity_remote_id)
            return

        if plan.action in (
            RecommendationAction.INCREASE_BUDGET,
            RecommendationAction.DECREASE_BUDGET,
        ):
            self._client.update_daily_budget(
                plan.entity_remote_id,
                daily_budget_minor=major_units_to_minor(plan.proposed_budget(), plan.currency),
            )
            return

        raise OptimizationError(
            "Recommendation describes an action that cannot be applied automatically",
            context={"action": plan.action.value, "entity_remote_id": plan.entity_remote_id},
        )


@dataclass(frozen=True, slots=True)
class _ApplicationPlan:
    """The facts needed to apply one recommendation, read before the API call.

    Extracted from the ORM instance inside the transaction so that the API call
    that follows does not touch a detached object.
    """

    entity_remote_id: str
    action: RecommendationAction
    suggested_change: dict[str, object]
    currency: str | None

    def proposed_budget(self) -> Decimal:
        """Parse the proposed daily budget from the stored change description.

        Returns:
            The proposed budget in major currency units.

        Raises:
            OptimizationError: If the stored change carries no usable value.
                Budgets are stored as strings to preserve exact decimals, so a
                malformed one must fail loudly rather than be coerced into a
                number that would be written to a live account.
        """
        raw_value = self.suggested_change.get("proposed_value")
        if raw_value is None:
            raise OptimizationError(
                "Recommendation has no proposed budget to apply",
                context={"entity_remote_id": self.entity_remote_id},
            )
        try:
            return Decimal(str(raw_value))
        except (InvalidOperation, ValueError) as exc:
            raise OptimizationError(
                "Recommendation carries an unparseable proposed budget",
                context={
                    "entity_remote_id": self.entity_remote_id,
                    "proposed_value": repr(raw_value),
                },
            ) from exc


def _load_entity_attributes(
    unit_of_work: UnitOfWork,
    account_id: int,
    level: InsightLevel,
) -> dict[str, EntityAttributes]:
    """Read budgets and delivery status for every entity at one level.

    One query per level rather than one per entity: an account with five hundred
    ad sets would otherwise issue five hundred round trips to answer a question
    the database can answer once.
    """
    if level is InsightLevel.CAMPAIGN:
        return {
            campaign.remote_id: EntityAttributes(
                daily_budget=campaign.daily_budget,
                is_delivering=campaign.is_delivering,
            )
            for campaign in unit_of_work.campaigns.list_for_account(account_id)
        }
    if level is InsightLevel.ADSET:
        return {
            ad_set.remote_id: EntityAttributes(
                daily_budget=ad_set.daily_budget,
                is_delivering=ad_set.is_delivering,
            )
            for ad_set in unit_of_work.ad_sets.list_for_account(account_id)
        }
    if level is InsightLevel.AD:
        return {
            ad.remote_id: EntityAttributes(daily_budget=None, is_delivering=ad.is_delivering)
            for ad in unit_of_work.ads.list_for_account(account_id)
        }
    return {}


def _attribute_for(
    attributes: dict[str, EntityAttributes],
    entity_remote_id: str,
) -> EntityAttributes:
    """Return an entity's attributes, defaulting to delivering with no budget.

    An entity with insight rows but no structural row was deleted from Meta
    between the two syncs. Assuming it delivers is the conservative default:
    rules that would advise pausing it still fire, and rules that would advise
    raising a budget cannot, because there is no budget to raise.
    """
    return attributes.get(entity_remote_id, EntityAttributes(daily_budget=None, is_delivering=True))


def _to_recommendation_model(
    context: RecommendationContext,
    proposal: RecommendationProposal,
    *,
    ad_account_id: int,
    generated_at: datetime,
) -> Recommendation:
    """Map a rule's proposal onto a persistable recommendation."""
    return Recommendation(
        ad_account_id=ad_account_id,
        level=context.level,
        entity_remote_id=context.entity_remote_id,
        entity_name=context.entity_name,
        rule_code=proposal.rule_code,
        severity=proposal.severity,
        action=proposal.action,
        title=proposal.title,
        rationale=proposal.rationale,
        metric_snapshot=proposal.metric_snapshot,
        suggested_change=proposal.suggested_change,
        generated_at=generated_at,
    )
