"""Derived advertising metrics computed from measured quantities.

This module is pure: no database, no HTTP, no logging side effects. It accepts
anything carrying the six measured fields — ORM rows, API payloads, or test
fixtures — through the :class:`MetricSource` protocol, which is what lets the
same definitions serve stored history and a live response without conversion.

Every derived metric is defined exactly once, here. Meta will compute CTR, CPC,
and CPM server-side on request, and those figures are deliberately not read:
Meta's per-row values cannot be averaged to obtain the value for a group of
rows, and an aggregate CTR computed as the mean of daily CTRs is simply wrong.
Deriving from summed measures gives the impression-weighted answer.

**Undefined metrics are ``None``, never zero.** A campaign with no clicks has no
cost per click. Returning ``Decimal(0)`` would make it sort as the cheapest
campaign in the account and would quietly corrupt any comparison it entered.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Protocol, runtime_checkable

# CTR and conversion rate are reported as percentages.
_PERCENT_MULTIPLIER: Final[Decimal] = Decimal(100)

# CPM is cost per *mille* — one thousand impressions.
_IMPRESSION_MILLE: Final[Decimal] = Decimal(1000)

_ZERO: Final[Decimal] = Decimal(0)


@runtime_checkable
class MetricSource(Protocol):
    """Anything carrying the measured quantities a metric is derived from.

    Structural rather than nominal so that
    :class:`~app.models.insight.InsightRecord` and
    :class:`~app.api.schemas.InsightsPayload` both satisfy it without this
    module importing either, keeping analytics free of persistence and
    transport dependencies.
    """

    @property
    def spend(self) -> Decimal:
        """Amount spent, in major currency units."""
        ...

    @property
    def impressions(self) -> int:
        """Times the ad was rendered."""
        ...

    @property
    def clicks(self) -> int:
        """Clicks received."""
        ...

    @property
    def reach(self) -> int:
        """Distinct people who saw the ad at least once."""
        ...

    @property
    def conversions(self) -> int:
        """Count of the optimized conversion event."""
        ...

    @property
    def conversion_value(self) -> Decimal:
        """Revenue attributed to those conversions."""
        ...


def safe_divide(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    """Divide, returning ``None`` when the result would be undefined.

    Args:
        numerator: Dividend.
        denominator: Divisor.

    Returns:
        The quotient, or ``None`` when the divisor is zero.
    """
    if denominator == _ZERO:
        return None
    return numerator / denominator


@dataclass(frozen=True, slots=True)
class PerformanceMetrics:
    """Measured quantities for one entity and window, plus everything derivable.

    Immutable, so a snapshot attached to a recommendation cannot be mutated by
    later aggregation and silently change what the recommendation claims to have
    been based on.

    Attributes:
        spend: Amount spent in major currency units.
        impressions: Times the ads were rendered.
        clicks: Clicks received.
        reach: Distinct people reached.
        conversions: Count of the optimized conversion event.
        conversion_value: Revenue attributed to those conversions.
        day_count: Days the window covers, used to derive daily averages.
    """

    spend: Decimal = _ZERO
    impressions: int = 0
    clicks: int = 0
    reach: int = 0
    conversions: int = 0
    conversion_value: Decimal = _ZERO
    day_count: int = 0

    @classmethod
    def from_sources(
        cls, sources: Iterable[MetricSource], *, day_count: int = 0
    ) -> PerformanceMetrics:
        """Sum measured quantities across many rows.

        Summing measures and deriving afterwards is the only correct order.
        Averaging per-row derived metrics weights a day with ten impressions
        equally against a day with a million.

        Note that summing ``reach`` overstates true reach, because the same
        person reached on two days is counted twice. Meta only reports
        deduplicated reach for a window it computes itself, so the sum is an
        upper bound; :attr:`frequency` derived from it is correspondingly a
        lower bound, which is the safe direction for a fatigue rule that fires
        on *high* frequency.

        Args:
            sources: Rows to aggregate.
            day_count: Days the combined window covers.

        Returns:
            The aggregated metrics.
        """
        spend = _ZERO
        impressions = 0
        clicks = 0
        reach = 0
        conversions = 0
        conversion_value = _ZERO

        for source in sources:
            spend += source.spend
            impressions += source.impressions
            clicks += source.clicks
            reach += source.reach
            conversions += source.conversions
            conversion_value += source.conversion_value

        return cls(
            spend=spend,
            impressions=impressions,
            clicks=clicks,
            reach=reach,
            conversions=conversions,
            conversion_value=conversion_value,
            day_count=day_count,
        )

    @property
    def has_delivery(self) -> bool:
        """Whether the entity delivered at all in this window."""
        return self.impressions > 0

    @property
    def click_through_rate(self) -> Decimal | None:
        """Clicks as a percentage of impressions, or ``None`` without delivery."""
        return _percentage(Decimal(self.clicks), Decimal(self.impressions))

    @property
    def cost_per_click(self) -> Decimal | None:
        """Spend per click, or ``None`` when there were no clicks."""
        return safe_divide(self.spend, Decimal(self.clicks))

    @property
    def cost_per_mille(self) -> Decimal | None:
        """Spend per thousand impressions, or ``None`` without delivery."""
        quotient = safe_divide(self.spend, Decimal(self.impressions))
        return None if quotient is None else quotient * _IMPRESSION_MILLE

    @property
    def cost_per_acquisition(self) -> Decimal | None:
        """Spend per conversion, or ``None`` when there were no conversions.

        The headline efficiency measure: what one result actually costs.
        """
        return safe_divide(self.spend, Decimal(self.conversions))

    @property
    def return_on_ad_spend(self) -> Decimal | None:
        """Revenue per unit of spend, or ``None`` when nothing was spent.

        A value of ``1`` means the campaign broke even on revenue, which is not
        the same as breaking even on profit.
        """
        return safe_divide(self.conversion_value, self.spend)

    @property
    def frequency(self) -> Decimal | None:
        """Average impressions per person reached, or ``None`` without reach.

        The fatigue signal: as the same people see an ad repeatedly, response
        falls while cost does not.
        """
        return safe_divide(Decimal(self.impressions), Decimal(self.reach))

    @property
    def conversion_rate(self) -> Decimal | None:
        """Conversions as a percentage of clicks, or ``None`` without clicks."""
        return _percentage(Decimal(self.conversions), Decimal(self.clicks))

    @property
    def average_daily_spend(self) -> Decimal | None:
        """Spend divided by the days covered, or ``None`` for an empty window.

        Compared against a daily budget, this reveals whether an entity is
        actually able to spend what it was given.
        """
        return safe_divide(self.spend, Decimal(self.day_count))

    def as_snapshot(self) -> dict[str, str | int | None]:
        """Render the metrics as a JSON-serializable mapping.

        Decimals are stringified rather than converted to float, because a
        snapshot stored alongside a recommendation is a record of what was
        measured and must not acquire binary rounding error on the way into the
        database.

        Returns:
            A mapping suitable for a JSON column.
        """
        return {
            "spend": str(self.spend),
            "impressions": self.impressions,
            "clicks": self.clicks,
            "reach": self.reach,
            "conversions": self.conversions,
            "conversion_value": str(self.conversion_value),
            "day_count": self.day_count,
            "ctr_percent": _optional_str(self.click_through_rate),
            "cpc": _optional_str(self.cost_per_click),
            "cpm": _optional_str(self.cost_per_mille),
            "cpa": _optional_str(self.cost_per_acquisition),
            "roas": _optional_str(self.return_on_ad_spend),
            "frequency": _optional_str(self.frequency),
        }


def _percentage(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    """Express ``numerator`` as a percentage of ``denominator``."""
    quotient = safe_divide(numerator, denominator)
    return None if quotient is None else quotient * _PERCENT_MULTIPLIER


def _optional_str(value: Decimal | None) -> str | None:
    """Stringify a Decimal, preserving ``None`` for undefined metrics."""
    return None if value is None else str(value)
