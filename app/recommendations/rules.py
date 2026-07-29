"""Rule-based optimization rules.

Each rule is a small, independent class implementing :class:`Rule`: given a
:class:`~app.recommendations.context.RecommendationContext`, it either returns a
proposal or returns ``None`` because it has nothing to say. Rules never query
anything, never mutate anything, and never know about each other.

That shape is what makes the engine extensible. An LLM-backed rule satisfies the
same protocol — receive a context, return a proposal or ``None`` — so it can be
added to the engine's rule list without the engine, the service, or the CLI
changing at all.

Every threshold is a field on :class:`RuleThresholds` with a named default. No
rule contains a bare number, so the entire policy of the system can be read in
one place and overridden per advertiser without touching rule logic.

Two guards recur throughout and are the difference between advice and noise:

*Sufficient evidence.* A campaign that spent four dollars and got no conversions
is not failing, it is young. Rules refuse to judge below a minimum spend or
impression count.

*Sufficient history.* Trend rules return ``None`` when no comparable prior
period exists, rather than treating a missing baseline as zero and reporting an
infinite deterioration on every new campaign's first week.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Protocol, runtime_checkable

from app.models.enums import RecommendationAction, RecommendationSeverity
from app.recommendations.context import RecommendationContext, RecommendationProposal

# --- default policy ---------------------------------------------------------
# Named so that no rule body contains an unexplained literal.

# Below this spend, an entity has not bought enough data to be judged.
_DEFAULT_MINIMUM_SPEND: Final[Decimal] = Decimal(50)

# Below this many impressions, a click-through rate is statistical noise.
_DEFAULT_MINIMUM_IMPRESSIONS: Final[int] = 1_000

# Below this many conversions, a cost per acquisition swings wildly on one sale.
_DEFAULT_MINIMUM_CONVERSIONS: Final[int] = 5

# Cost per acquisition rising by this much period over period is a real
# regression rather than ordinary auction noise.
_DEFAULT_CPA_INCREASE_PERCENT: Final[Decimal] = Decimal(25)

# Click-through rate falling by this much alongside high frequency is the
# classic signature of creative fatigue.
_DEFAULT_CTR_DECLINE_PERCENT: Final[Decimal] = Decimal(25)

# Average impressions per person at which an audience is being over-exposed.
_DEFAULT_FATIGUE_FREQUENCY: Final[Decimal] = Decimal(3)

# A click-through rate beneath this, with adequate impressions, indicates a
# mismatch between the ad and the audience it is being shown to.
_DEFAULT_LOW_CTR_PERCENT: Final[Decimal] = Decimal("0.5")

# Return on ad spend at or above which an entity is worth funding further.
_DEFAULT_TARGET_ROAS: Final[Decimal] = Decimal(2)

# How much to raise a winning budget. Deliberately modest: Meta's delivery
# system re-enters the learning phase after a large budget change, which can
# undo the very performance being scaled.
_DEFAULT_BUDGET_INCREASE_PERCENT: Final[Decimal] = Decimal(20)

# How much to cut an underperforming budget.
_DEFAULT_BUDGET_DECREASE_PERCENT: Final[Decimal] = Decimal(25)

# Spending less than this share of the daily budget means delivery, not
# efficiency, is the binding constraint.
_DEFAULT_UNDERSPEND_RATIO: Final[Decimal] = Decimal("0.7")

# A deterioration this many times the threshold is treated as critical.
_CRITICAL_SEVERITY_MULTIPLIER: Final[Decimal] = Decimal(2)

_PERCENT_BASE: Final[Decimal] = Decimal(100)
_MONEY_QUANTUM: Final[Decimal] = Decimal("0.01")
_RATIO_QUANTUM: Final[Decimal] = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class RuleThresholds:
    """The complete policy the rule set is evaluated against.

    Injected rather than read from module constants so that a future
    per-advertiser policy requires no change to any rule. An account selling
    sofas and an account selling software do not share a sensible target return
    on ad spend.
    """

    minimum_spend: Decimal = _DEFAULT_MINIMUM_SPEND
    minimum_impressions: int = _DEFAULT_MINIMUM_IMPRESSIONS
    minimum_conversions: int = _DEFAULT_MINIMUM_CONVERSIONS
    cpa_increase_percent: Decimal = _DEFAULT_CPA_INCREASE_PERCENT
    ctr_decline_percent: Decimal = _DEFAULT_CTR_DECLINE_PERCENT
    fatigue_frequency: Decimal = _DEFAULT_FATIGUE_FREQUENCY
    low_ctr_percent: Decimal = _DEFAULT_LOW_CTR_PERCENT
    target_roas: Decimal = _DEFAULT_TARGET_ROAS
    budget_increase_percent: Decimal = _DEFAULT_BUDGET_INCREASE_PERCENT
    budget_decrease_percent: Decimal = _DEFAULT_BUDGET_DECREASE_PERCENT
    underspend_ratio: Decimal = _DEFAULT_UNDERSPEND_RATIO


@runtime_checkable
class Rule(Protocol):
    """One independent judgement about an entity's performance.

    Implementations must be pure functions of their inputs. The engine may call
    them in any order and makes no guarantee about how often.
    """

    @property
    def code(self) -> str:
        """Stable identifier, used to deduplicate stored recommendations."""
        ...

    def evaluate(
        self,
        context: RecommendationContext,
        thresholds: RuleThresholds,
    ) -> RecommendationProposal | None:
        """Judge one entity.

        Args:
            context: Everything known about the entity.
            thresholds: The policy to judge against.

        Returns:
            A proposal, or ``None`` when the rule has nothing to say.
        """
        ...


class ZeroConversionSpendRule:
    """Flags entities spending meaningfully while producing no conversions.

    The most expensive failure in an ad account and the easiest to miss, because
    a campaign with no results generates no notifications. Requires a minimum
    spend so that a campaign one day old is not condemned.

    Note the rationale names conversion tracking explicitly. Zero conversions on
    real spend is as often a broken pixel as a bad campaign, and pausing a
    profitable campaign because its tracking broke is worse than the problem.
    """

    @property
    def code(self) -> str:
        """Stable identifier for this rule."""
        return "zero_conversion_spend"

    def evaluate(
        self,
        context: RecommendationContext,
        thresholds: RuleThresholds,
    ) -> RecommendationProposal | None:
        """Return a proposal when spend produced no conversions at all."""
        metrics = context.current
        if not context.is_delivering:
            return None
        if metrics.spend < thresholds.minimum_spend:
            return None
        if metrics.conversions > 0:
            return None

        spend_text = _format_money(metrics.spend, context.currency)
        return RecommendationProposal(
            rule_code=self.code,
            severity=RecommendationSeverity.CRITICAL,
            action=RecommendationAction.PAUSE_ENTITY,
            title=f"{_entity_label(context)} spent {spend_text} with no conversions",
            rationale=(
                f"{spend_text} was spent across {metrics.impressions:,} impressions and "
                f"{metrics.clicks:,} clicks without recording a single conversion. "
                f"Verify that conversion tracking is reporting correctly before pausing: "
                f"a broken pixel produces exactly this pattern on a campaign that is in "
                f"fact profitable. If tracking is healthy, this spend is not returning "
                f"anything and should stop."
            ),
            metric_snapshot=dict(metrics.as_snapshot()),
            suggested_change={
                "field": "status",
                "current_value": "active",
                "proposed_value": "paused",
            },
        )


class RisingCostPerAcquisitionRule:
    """Flags entities whose cost per conversion has materially worsened.

    Requires enough conversions in the prior period for the baseline to mean
    something. With two conversions last week, one fewer this week moves cost
    per acquisition by fifty percent for reasons that are pure chance.
    """

    @property
    def code(self) -> str:
        """Stable identifier for this rule."""
        return "rising_cost_per_acquisition"

    def evaluate(
        self,
        context: RecommendationContext,
        thresholds: RuleThresholds,
    ) -> RecommendationProposal | None:
        """Return a proposal when cost per acquisition rose beyond tolerance."""
        comparison = context.comparison
        if comparison is None or not context.is_delivering:
            return None
        if comparison.previous.conversions < thresholds.minimum_conversions:
            return None
        if context.current.spend < thresholds.minimum_spend:
            return None

        change = comparison.cost_per_acquisition
        if not change.worsened_by_at_least(thresholds.cpa_increase_percent):
            return None

        percent_change = change.percent_change
        if percent_change is None:
            return None

        severity = (
            RecommendationSeverity.CRITICAL
            if percent_change >= thresholds.cpa_increase_percent * _CRITICAL_SEVERITY_MULTIPLIER
            else RecommendationSeverity.WARNING
        )
        previous_text = _format_money(change.previous, context.currency)
        current_text = _format_money(change.current, context.currency)

        return RecommendationProposal(
            rule_code=self.code,
            severity=severity,
            action=RecommendationAction.DECREASE_BUDGET,
            title=(
                f"{_entity_label(context)} cost per result rose "
                f"{_format_percent(percent_change)} to {current_text}"
            ),
            rationale=(
                f"Cost per conversion moved from {previous_text} to {current_text}, an "
                f"increase of {_format_percent(percent_change)}, while the entity remained "
                f"active. The prior period recorded {comparison.previous.conversions:,} "
                f"conversions, so the baseline is not an artefact of low volume. Reducing "
                f"spend limits the cost of the regression while its cause is investigated."
            ),
            metric_snapshot=dict(context.current.as_snapshot()),
            suggested_change=_budget_change(
                context,
                multiplier=_PERCENT_BASE - thresholds.budget_decrease_percent,
            ),
        )


class CreativeFatigueRule:
    """Flags audiences seeing an ad too often while responding to it less.

    Neither signal alone is conclusive. High frequency in a small, valuable
    retargeting audience is intentional, and a falling click-through rate during
    a seasonal shift affects everyone. Together they are the standard signature
    of an audience that has simply seen the ad too many times.
    """

    @property
    def code(self) -> str:
        """Stable identifier for this rule."""
        return "creative_fatigue"

    def evaluate(
        self,
        context: RecommendationContext,
        thresholds: RuleThresholds,
    ) -> RecommendationProposal | None:
        """Return a proposal when high frequency coincides with falling CTR."""
        comparison = context.comparison
        if comparison is None or not context.is_delivering:
            return None
        if context.current.impressions < thresholds.minimum_impressions:
            return None

        frequency = context.current.frequency
        if frequency is None or frequency < thresholds.fatigue_frequency:
            return None

        ctr_change = comparison.click_through_rate
        if not ctr_change.declined_by_at_least(thresholds.ctr_decline_percent):
            return None

        percent_change = ctr_change.percent_change
        if percent_change is None:
            return None

        return RecommendationProposal(
            rule_code=self.code,
            severity=RecommendationSeverity.WARNING,
            action=RecommendationAction.ROTATE_CREATIVE,
            title=(
                f"{_entity_label(context)} shows creative fatigue at "
                f"{_format_ratio(frequency)}x frequency"
            ),
            rationale=(
                f"The average person in this audience has now seen the ad "
                f"{_format_ratio(frequency)} times, and click-through rate has fallen "
                f"{_format_percent(abs(percent_change))} from "
                f"{_format_ratio(ctr_change.previous)}% to "
                f"{_format_ratio(ctr_change.current)}%. Rising exposure with falling "
                f"response is creative fatigue rather than an audience problem. Refreshing "
                f"the creative typically recovers performance; increasing budget against a "
                f"fatigued creative accelerates the decline."
            ),
            metric_snapshot=dict(context.current.as_snapshot()),
        )


class LowClickThroughRateRule:
    """Flags ads shown widely that almost nobody clicks.

    Distinguished from creative fatigue by needing no history: this is an ad
    that never resonated, rather than one that stopped resonating.
    """

    @property
    def code(self) -> str:
        """Stable identifier for this rule."""
        return "low_click_through_rate"

    def evaluate(
        self,
        context: RecommendationContext,
        thresholds: RuleThresholds,
    ) -> RecommendationProposal | None:
        """Return a proposal when CTR is low despite adequate impressions."""
        metrics = context.current
        if not context.is_delivering:
            return None
        if metrics.impressions < thresholds.minimum_impressions:
            return None

        click_through_rate = metrics.click_through_rate
        if click_through_rate is None or click_through_rate >= thresholds.low_ctr_percent:
            return None

        return RecommendationProposal(
            rule_code=self.code,
            severity=RecommendationSeverity.INFO,
            action=RecommendationAction.REVIEW_TARGETING,
            title=(
                f"{_entity_label(context)} click-through rate is "
                f"{_format_ratio(click_through_rate)}%"
            ),
            rationale=(
                f"Across {metrics.impressions:,} impressions the ad drew "
                f"{metrics.clicks:,} clicks, a rate of "
                f"{_format_ratio(click_through_rate)}%, below the "
                f"{_format_ratio(thresholds.low_ctr_percent)}% floor. At this volume the "
                f"figure is not noise. Either the audience is wrong for the offer or the "
                f"creative is not communicating it; both are worth reviewing before more "
                f"budget is committed."
            ),
            metric_snapshot=dict(metrics.as_snapshot()),
        )


class ScaleWinnerRule:
    """Flags profitable entities that are worth funding further.

    The counterpart to the failure rules: an account manager who only ever cuts
    losers slowly starves the account. Requires a budget at this level, because
    advice to raise a budget that lives elsewhere cannot be acted on.

    The proposed increase is intentionally modest. A large budget change pushes
    Meta's delivery system back into its learning phase, which can destroy the
    performance being scaled.
    """

    @property
    def code(self) -> str:
        """Stable identifier for this rule."""
        return "scale_winner"

    def evaluate(
        self,
        context: RecommendationContext,
        thresholds: RuleThresholds,
    ) -> RecommendationProposal | None:
        """Return a proposal when returns justify a larger budget."""
        metrics = context.current
        if not context.is_delivering or context.daily_budget is None:
            return None
        if metrics.conversions < thresholds.minimum_conversions:
            return None
        if metrics.spend < thresholds.minimum_spend:
            return None

        roas = metrics.return_on_ad_spend
        if roas is None or roas < thresholds.target_roas:
            return None

        change = _budget_change(
            context,
            multiplier=_PERCENT_BASE + thresholds.budget_increase_percent,
        )
        if not change:
            return None

        return RecommendationProposal(
            rule_code=self.code,
            severity=RecommendationSeverity.INFO,
            action=RecommendationAction.INCREASE_BUDGET,
            title=(
                f"{_entity_label(context)} returns {_format_ratio(roas)}x and can absorb "
                f"more budget"
            ),
            rationale=(
                f"{_format_money(metrics.spend, context.currency)} produced "
                f"{_format_money(metrics.conversion_value, context.currency)} across "
                f"{metrics.conversions:,} conversions, a return of "
                f"{_format_ratio(roas)}x against a target of "
                f"{_format_ratio(thresholds.target_roas)}x. Raising the daily budget by "
                f"{_format_percent(thresholds.budget_increase_percent)} tests whether the "
                f"result holds at higher volume. The step is small on purpose: a large "
                f"budget change returns delivery to the learning phase and can undo the "
                f"performance being scaled."
            ),
            metric_snapshot=dict(metrics.as_snapshot()),
            suggested_change=change,
        )


class BudgetUnderspendRule:
    """Flags entities unable to spend the budget they were given.

    A signal that delivery, not efficiency, is the binding constraint —
    typically an audience too narrow, a bid cap set too low, or a schedule too
    restrictive. Raising the budget cannot help, and this is the rule that stops
    someone from trying.
    """

    @property
    def code(self) -> str:
        """Stable identifier for this rule."""
        return "budget_underspend"

    def evaluate(
        self,
        context: RecommendationContext,
        thresholds: RuleThresholds,
    ) -> RecommendationProposal | None:
        """Return a proposal when daily spend falls well short of the budget."""
        metrics = context.current
        budget = context.daily_budget
        if not context.is_delivering or budget is None or budget <= Decimal(0):
            return None
        if not metrics.has_delivery:
            return None

        average_daily_spend = metrics.average_daily_spend
        if average_daily_spend is None:
            return None

        utilisation = average_daily_spend / budget
        if utilisation >= thresholds.underspend_ratio:
            return None

        return RecommendationProposal(
            rule_code=self.code,
            severity=RecommendationSeverity.INFO,
            action=RecommendationAction.REVIEW_TARGETING,
            title=(
                f"{_entity_label(context)} used "
                f"{_format_percent(utilisation * _PERCENT_BASE)} of its daily budget"
            ),
            rationale=(
                f"Average daily spend was "
                f"{_format_money(average_daily_spend, context.currency)} against a budget of "
                f"{_format_money(budget, context.currency)}, a utilisation of "
                f"{_format_percent(utilisation * _PERCENT_BASE)}. The constraint is "
                f"delivery rather than budget, so raising the budget will change nothing. "
                f"Check for an audience that is too narrow, a bid cap set below the "
                f"clearing price, or a restrictive schedule."
            ),
            metric_snapshot=dict(metrics.as_snapshot()),
        )


def default_rules() -> tuple[Rule, ...]:
    """Return the standard rule set, in no significant order.

    Ordering is irrelevant because rules are independent; the engine sorts
    findings by severity after collecting them.

    Returns:
        One instance of every built-in rule.
    """
    return (
        ZeroConversionSpendRule(),
        RisingCostPerAcquisitionRule(),
        CreativeFatigueRule(),
        LowClickThroughRateRule(),
        ScaleWinnerRule(),
        BudgetUnderspendRule(),
    )


def _budget_change(context: RecommendationContext, *, multiplier: Decimal) -> dict[str, object]:
    """Describe a proportional daily budget change, if there is a budget to change.

    Args:
        context: The entity under evaluation.
        multiplier: Target as a percentage of the current budget, e.g. ``120``
            for a twenty percent increase.

    Returns:
        A machine-readable change description, or an empty mapping when no
        budget is held at this level — in which case the recommendation remains
        advisory and cannot be applied automatically.
    """
    budget = context.daily_budget
    if budget is None or budget <= Decimal(0):
        return {}
    proposed = (budget * multiplier / _PERCENT_BASE).quantize(_MONEY_QUANTUM)
    return {
        "field": "daily_budget",
        "current_value": str(budget),
        "proposed_value": str(proposed),
        "currency": context.currency,
    }


def _entity_label(context: RecommendationContext) -> str:
    """Render the entity's name, falling back to its Meta ID."""
    return context.entity_name or context.entity_remote_id


def _format_money(value: Decimal | None, currency: str | None) -> str:
    """Render a monetary amount with its currency code."""
    if value is None:
        return "n/a"
    amount = value.quantize(_MONEY_QUANTUM)
    return f"{amount} {currency}" if currency else str(amount)


def _format_percent(value: Decimal | None) -> str:
    """Render a percentage to one decimal place."""
    if value is None:
        return "n/a"
    return f"{value.quantize(Decimal('0.1'))}%"


def _format_ratio(value: Decimal | None) -> str:
    """Render a bare ratio to two decimal places."""
    if value is None:
        return "n/a"
    return str(value.quantize(_RATIO_QUANTUM))
