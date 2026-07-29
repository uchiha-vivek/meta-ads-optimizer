"""Inputs and outputs of the recommendation engine.

These types are the contract between the service layer, which knows how to
gather data, and the rules, which know how to judge it. Keeping them separate
from both means a rule can be evaluated in a test by constructing a context
literal — no database, no API, no fixtures.

:class:`RecommendationProposal` is deliberately not the ORM model. A rule
produces a value object describing its finding; deciding whether that finding is
new, supersedes an earlier one, or should be persisted at all belongs to the
service. A rule that returned an ORM instance would have to know about sessions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.analytics.metrics import PerformanceMetrics
from app.analytics.trends import PeriodComparison
from app.models.enums import InsightLevel, RecommendationAction, RecommendationSeverity


@dataclass(frozen=True, slots=True)
class RecommendationContext:
    """Everything a rule may consider about one entity.

    A rule receives this and nothing else. It cannot query the database or call
    the API, which is what makes rule evaluation deterministic and instant to
    test.

    Attributes:
        level: Whether this describes a campaign, ad set, or ad.
        entity_remote_id: Meta ID of the entity under evaluation.
        entity_name: Human-readable name, for the recommendation text.
        currency: ISO 4217 code the monetary figures are expressed in.
        current: Metrics for the window under examination.
        comparison: The same window set against the preceding one, or ``None``
            when insufficient history exists. Rules that reason about trends
            must return ``None`` in that case rather than inventing a baseline.
        daily_budget: Budget in force at this level, in major currency units, or
            ``None`` when the budget is held at another level.
        is_delivering: Whether the advertiser has this entity configured to
            spend. Advice to pause something already paused is noise.
        creative_age_days: Days since the ad's creative was first seen, when
            known. Fatigue is a function of exposure over time.
    """

    level: InsightLevel
    entity_remote_id: str
    entity_name: str | None
    currency: str | None
    current: PerformanceMetrics
    comparison: PeriodComparison | None = None
    daily_budget: Decimal | None = None
    is_delivering: bool = True
    creative_age_days: int | None = None


@dataclass(frozen=True, slots=True)
class RecommendationProposal:
    """A finding produced by one rule about one entity.

    Attributes:
        rule_code: Stable identifier of the rule that produced this. Used to
            deduplicate against previously stored advice, so it must not change
            once released.
        severity: How urgently this deserves attention.
        action: The concrete change proposed.
        title: One-line summary, shown in the Rich table.
        rationale: The reasoning, including the figures that triggered it. Read
            by a human deciding whether to act.
        metric_snapshot: The measurements behind the finding, JSON-serializable.
        suggested_change: Machine-readable description of the mutation, used by
            ``meta optimize --apply``. Empty for advisory findings.
    """

    rule_code: str
    severity: RecommendationSeverity
    action: RecommendationAction
    title: str
    rationale: str
    metric_snapshot: dict[str, Any] = field(default_factory=dict)
    suggested_change: dict[str, Any] = field(default_factory=dict)

    @property
    def is_automatable(self) -> bool:
        """Whether this proposal can be applied through the API unattended."""
        return self.action.is_automatable and bool(self.suggested_change)
