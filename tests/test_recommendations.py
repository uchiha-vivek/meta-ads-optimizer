"""Tests for the rule set and the recommendation engine."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.analytics.metrics import PerformanceMetrics
from app.analytics.trends import PeriodComparison
from app.models.enums import InsightLevel, RecommendationAction, RecommendationSeverity
from app.recommendations.context import RecommendationContext, RecommendationProposal
from app.recommendations.engine import RecommendationEngine
from app.recommendations.rules import (
    BudgetUnderspendRule,
    CreativeFatigueRule,
    LowClickThroughRateRule,
    RisingCostPerAcquisitionRule,
    Rule,
    RuleThresholds,
    ScaleWinnerRule,
    ZeroConversionSpendRule,
    default_rules,
)

THRESHOLDS = RuleThresholds()


def make_context(
    *,
    current: PerformanceMetrics,
    previous: PerformanceMetrics | None = None,
    daily_budget: Decimal | None = None,
    is_delivering: bool = True,
) -> RecommendationContext:
    """Build a context for one entity, with an optional prior period."""
    comparison = (
        PeriodComparison(current=current, previous=previous) if previous is not None else None
    )
    return RecommendationContext(
        level=InsightLevel.CAMPAIGN,
        entity_remote_id="c1",
        entity_name="Spring Sale",
        currency="USD",
        current=current,
        comparison=comparison,
        daily_budget=daily_budget,
        is_delivering=is_delivering,
    )


# ---------------------------------------------------------------------------
# ZeroConversionSpendRule
# ---------------------------------------------------------------------------


def test_zero_conversion_spend_fires_on_meaningful_wasted_spend() -> None:
    context = make_context(
        current=PerformanceMetrics(
            spend=Decimal(500), impressions=20_000, clicks=300, conversions=0
        )
    )

    proposal = ZeroConversionSpendRule().evaluate(context, THRESHOLDS)

    assert proposal is not None
    assert proposal.severity is RecommendationSeverity.CRITICAL
    assert proposal.action is RecommendationAction.PAUSE_ENTITY
    # Zero conversions is as often a broken pixel as a bad campaign; pausing a
    # profitable campaign over a tracking fault would be worse than the problem.
    assert "tracking" in proposal.rationale.lower()


def test_zero_conversion_spend_abstains_below_the_minimum_spend() -> None:
    # A campaign that spent four dollars is young, not failing.
    context = make_context(
        current=PerformanceMetrics(spend=Decimal(4), impressions=100, conversions=0)
    )

    assert ZeroConversionSpendRule().evaluate(context, THRESHOLDS) is None


def test_zero_conversion_spend_abstains_when_conversions_exist() -> None:
    context = make_context(
        current=PerformanceMetrics(spend=Decimal(500), impressions=20_000, conversions=1)
    )

    assert ZeroConversionSpendRule().evaluate(context, THRESHOLDS) is None


def test_zero_conversion_spend_abstains_on_a_paused_entity() -> None:
    # Advice to pause something already paused is noise.
    context = make_context(
        current=PerformanceMetrics(spend=Decimal(500), impressions=20_000, conversions=0),
        is_delivering=False,
    )

    assert ZeroConversionSpendRule().evaluate(context, THRESHOLDS) is None


# ---------------------------------------------------------------------------
# RisingCostPerAcquisitionRule
# ---------------------------------------------------------------------------


def test_rising_cpa_fires_when_cost_worsens_beyond_tolerance() -> None:
    context = make_context(
        current=PerformanceMetrics(spend=Decimal(400), impressions=10_000, conversions=10),
        previous=PerformanceMetrics(spend=Decimal(200), impressions=10_000, conversions=10),
        daily_budget=Decimal(100),
    )

    proposal = RisingCostPerAcquisitionRule().evaluate(context, THRESHOLDS)

    assert proposal is not None
    assert proposal.action is RecommendationAction.DECREASE_BUDGET
    # 100% worse is at least twice the 25% threshold, so it escalates.
    assert proposal.severity is RecommendationSeverity.CRITICAL
    assert proposal.suggested_change["proposed_value"] == "75.00"


def test_rising_cpa_is_a_warning_for_a_modest_regression() -> None:
    context = make_context(
        current=PerformanceMetrics(spend=Decimal(260), impressions=10_000, conversions=10),
        previous=PerformanceMetrics(spend=Decimal(200), impressions=10_000, conversions=10),
        daily_budget=Decimal(100),
    )

    proposal = RisingCostPerAcquisitionRule().evaluate(context, THRESHOLDS)

    assert proposal is not None
    assert proposal.severity is RecommendationSeverity.WARNING


def test_rising_cpa_abstains_without_history() -> None:
    # No baseline must mean no judgement, not a baseline of zero.
    context = make_context(
        current=PerformanceMetrics(spend=Decimal(400), impressions=10_000, conversions=10)
    )

    assert RisingCostPerAcquisitionRule().evaluate(context, THRESHOLDS) is None


def test_rising_cpa_abstains_when_the_baseline_is_too_thin() -> None:
    # With two prior conversions, one fewer swings CPA by 50% for reasons that
    # are pure chance.
    context = make_context(
        current=PerformanceMetrics(spend=Decimal(400), impressions=10_000, conversions=2),
        previous=PerformanceMetrics(spend=Decimal(100), impressions=10_000, conversions=2),
    )

    assert RisingCostPerAcquisitionRule().evaluate(context, THRESHOLDS) is None


def test_rising_cpa_without_a_budget_is_advisory_only() -> None:
    context = make_context(
        current=PerformanceMetrics(spend=Decimal(400), impressions=10_000, conversions=10),
        previous=PerformanceMetrics(spend=Decimal(200), impressions=10_000, conversions=10),
        daily_budget=None,
    )

    proposal = RisingCostPerAcquisitionRule().evaluate(context, THRESHOLDS)

    assert proposal is not None
    # A budget held at another level cannot be changed here.
    assert proposal.suggested_change == {}
    assert proposal.is_automatable is False


# ---------------------------------------------------------------------------
# CreativeFatigueRule
# ---------------------------------------------------------------------------


def test_creative_fatigue_needs_both_high_frequency_and_falling_ctr() -> None:
    context = make_context(
        current=PerformanceMetrics(
            spend=Decimal(300), impressions=40_000, clicks=120, reach=10_000
        ),
        previous=PerformanceMetrics(
            spend=Decimal(300), impressions=40_000, clicks=400, reach=20_000
        ),
    )

    proposal = CreativeFatigueRule().evaluate(context, THRESHOLDS)

    assert proposal is not None
    assert proposal.action is RecommendationAction.ROTATE_CREATIVE
    # Rotating a creative needs human judgement, so it is never auto-applied.
    assert proposal.is_automatable is False


def test_creative_fatigue_abstains_when_frequency_is_low() -> None:
    # A falling CTR alone may just be a seasonal shift affecting everyone.
    context = make_context(
        current=PerformanceMetrics(spend=Decimal(300), impressions=20_000, clicks=60, reach=20_000),
        previous=PerformanceMetrics(
            spend=Decimal(300), impressions=20_000, clicks=400, reach=20_000
        ),
    )

    assert CreativeFatigueRule().evaluate(context, THRESHOLDS) is None


def test_creative_fatigue_abstains_when_ctr_holds_up() -> None:
    # High frequency alone is intentional in a small retargeting audience.
    context = make_context(
        current=PerformanceMetrics(
            spend=Decimal(300), impressions=40_000, clicks=400, reach=10_000
        ),
        previous=PerformanceMetrics(
            spend=Decimal(300), impressions=40_000, clicks=400, reach=10_000
        ),
    )

    assert CreativeFatigueRule().evaluate(context, THRESHOLDS) is None


# ---------------------------------------------------------------------------
# LowClickThroughRateRule
# ---------------------------------------------------------------------------


def test_low_ctr_fires_with_adequate_impressions() -> None:
    context = make_context(
        current=PerformanceMetrics(spend=Decimal(100), impressions=50_000, clicks=100)
    )

    proposal = LowClickThroughRateRule().evaluate(context, THRESHOLDS)

    assert proposal is not None
    assert proposal.action is RecommendationAction.REVIEW_TARGETING
    assert proposal.severity is RecommendationSeverity.INFO


def test_low_ctr_abstains_when_impressions_are_too_few_to_mean_anything() -> None:
    context = make_context(
        current=PerformanceMetrics(spend=Decimal(100), impressions=100, clicks=0)
    )

    assert LowClickThroughRateRule().evaluate(context, THRESHOLDS) is None


# ---------------------------------------------------------------------------
# ScaleWinnerRule
# ---------------------------------------------------------------------------


def test_scale_winner_proposes_a_modest_increase() -> None:
    context = make_context(
        current=PerformanceMetrics(
            spend=Decimal(500),
            impressions=50_000,
            clicks=1_000,
            conversions=25,
            conversion_value=Decimal(2_000),
        ),
        daily_budget=Decimal(100),
    )

    proposal = ScaleWinnerRule().evaluate(context, THRESHOLDS)

    assert proposal is not None
    assert proposal.action is RecommendationAction.INCREASE_BUDGET
    # Small on purpose: a large change re-enters Meta's learning phase and can
    # undo the performance being scaled.
    assert proposal.suggested_change["proposed_value"] == "120.00"
    assert proposal.is_automatable is True


def test_scale_winner_abstains_without_a_budget_at_this_level() -> None:
    context = make_context(
        current=PerformanceMetrics(
            spend=Decimal(500),
            impressions=50_000,
            conversions=25,
            conversion_value=Decimal(2_000),
        ),
        daily_budget=None,
    )

    assert ScaleWinnerRule().evaluate(context, THRESHOLDS) is None


def test_scale_winner_abstains_below_the_target_return() -> None:
    context = make_context(
        current=PerformanceMetrics(
            spend=Decimal(500),
            impressions=50_000,
            conversions=25,
            conversion_value=Decimal(600),
        ),
        daily_budget=Decimal(100),
    )

    assert ScaleWinnerRule().evaluate(context, THRESHOLDS) is None


# ---------------------------------------------------------------------------
# BudgetUnderspendRule
# ---------------------------------------------------------------------------


def test_budget_underspend_fires_when_delivery_is_the_constraint() -> None:
    context = make_context(
        current=PerformanceMetrics(spend=Decimal(140), impressions=10_000, clicks=100, day_count=7),
        daily_budget=Decimal(100),
    )

    proposal = BudgetUnderspendRule().evaluate(context, THRESHOLDS)

    assert proposal is not None
    # Raising the budget cannot help; this is the rule that stops someone trying.
    assert proposal.action is RecommendationAction.REVIEW_TARGETING


def test_budget_underspend_abstains_when_the_budget_is_being_used() -> None:
    context = make_context(
        current=PerformanceMetrics(spend=Decimal(660), impressions=10_000, clicks=100, day_count=7),
        daily_budget=Decimal(100),
    )

    assert BudgetUnderspendRule().evaluate(context, THRESHOLDS) is None


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


def test_engine_exposes_every_default_rule_code() -> None:
    engine = RecommendationEngine()

    assert len(engine.rule_codes) == len(default_rules())
    assert "zero_conversion_spend" in engine.rule_codes
    # Codes are stored to deduplicate advice, so they must be unique.
    assert len(set(engine.rule_codes)) == len(engine.rule_codes)


def test_engine_orders_findings_by_severity() -> None:
    engine = RecommendationEngine()
    context = make_context(
        current=PerformanceMetrics(
            spend=Decimal(500), impressions=50_000, clicks=100, reach=20_000, conversions=0
        ),
        daily_budget=Decimal(100),
    )

    proposals = engine.evaluate(context)

    severities = [proposal.severity.rank for proposal in proposals]
    assert severities == sorted(severities, reverse=True)
    assert proposals[0].severity is RecommendationSeverity.CRITICAL


def test_engine_returns_nothing_for_a_healthy_entity() -> None:
    engine = RecommendationEngine()
    context = make_context(
        current=PerformanceMetrics(
            spend=Decimal(690),
            impressions=50_000,
            clicks=1_000,
            reach=40_000,
            conversions=20,
            conversion_value=Decimal(1_000),
            day_count=7,
        ),
        previous=PerformanceMetrics(
            spend=Decimal(690),
            impressions=50_000,
            clicks=1_000,
            reach=40_000,
            conversions=20,
            conversion_value=Decimal(1_000),
            day_count=7,
        ),
        daily_budget=Decimal(100),
    )

    assert engine.evaluate(context) == []


class ExplodingRule:
    """A rule that always raises, standing in for contributed or generated code."""

    @property
    def code(self) -> str:
        return "exploding"

    def evaluate(
        self,
        context: RecommendationContext,
        thresholds: RuleThresholds,
    ) -> RecommendationProposal | None:
        message = "this rule is broken"
        raise RuntimeError(message)


def test_one_failing_rule_does_not_lose_the_others() -> None:
    engine = RecommendationEngine(
        rules=[ExplodingRule(), ZeroConversionSpendRule()],
    )
    context = make_context(
        current=PerformanceMetrics(spend=Decimal(500), impressions=20_000, conversions=0)
    )

    proposals = engine.evaluate(context)

    # Losing every finding across every entity because one rule raised would be
    # far worse than that rule's silence.
    assert len(proposals) == 1
    assert proposals[0].rule_code == "zero_conversion_spend"


def test_evaluate_all_pairs_findings_with_their_context() -> None:
    engine = RecommendationEngine(rules=[ZeroConversionSpendRule()])
    context = make_context(
        current=PerformanceMetrics(spend=Decimal(500), impressions=20_000, conversions=0)
    )

    paired = engine.evaluate_all([context])

    assert len(paired) == 1
    # The proposal alone does not carry the entity's identity.
    assert paired[0][0].entity_remote_id == "c1"


def test_thresholds_are_injectable() -> None:
    lenient = RuleThresholds(minimum_spend=Decimal(10_000))
    engine = RecommendationEngine(rules=[ZeroConversionSpendRule()], thresholds=lenient)
    context = make_context(
        current=PerformanceMetrics(spend=Decimal(500), impressions=20_000, conversions=0)
    )

    assert engine.evaluate(context) == []


@pytest.mark.parametrize("rule", default_rules())
def test_every_default_rule_satisfies_the_protocol(rule: Rule) -> None:
    # The seam an LLM-backed rule will plug into later.
    assert isinstance(rule, Rule)
    assert rule.code
