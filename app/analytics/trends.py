"""Period-over-period comparison of performance metrics.

Absolute figures rarely justify an intervention on their own. A cost per
acquisition of $42 is either excellent or alarming depending entirely on what it
was last week, which is the comparison this module expresses and the reason the
project stores history at all.

Like :mod:`app.analytics.metrics`, this is pure computation over values.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Final

from app.analytics.metrics import MetricSource, PerformanceMetrics, safe_divide

_PERCENT_MULTIPLIER: Final[Decimal] = Decimal(100)
_ZERO: Final[Decimal] = Decimal(0)


class ChangeDirection(StrEnum):
    """Which way a metric moved between two periods."""

    UP = "up"
    DOWN = "down"
    FLAT = "flat"
    UNDEFINED = "undefined"


@dataclass(frozen=True, slots=True)
class MetricChange:
    """How one metric moved between a previous and a current period.

    Attributes:
        current: Value in the current period, or ``None`` when undefined.
        previous: Value in the prior period, or ``None`` when undefined.
    """

    current: Decimal | None
    previous: Decimal | None

    @property
    def absolute_change(self) -> Decimal | None:
        """Current minus previous, or ``None`` when either side is undefined."""
        if self.current is None or self.previous is None:
            return None
        return self.current - self.previous

    @property
    def percent_change(self) -> Decimal | None:
        """Change as a percentage of the previous value.

        ``None`` when either side is undefined or the previous value is zero.
        A move from zero is not a percentage increase of any size; describing it
        as "infinite growth" would let a rule fire on the first day of any new
        campaign.
        """
        if self.current is None or self.previous is None or self.previous == _ZERO:
            return None
        ratio = safe_divide(self.current - self.previous, abs(self.previous))
        return None if ratio is None else ratio * _PERCENT_MULTIPLIER

    @property
    def direction(self) -> ChangeDirection:
        """Which way the metric moved."""
        change = self.absolute_change
        if change is None:
            return ChangeDirection.UNDEFINED
        if change > _ZERO:
            return ChangeDirection.UP
        if change < _ZERO:
            return ChangeDirection.DOWN
        return ChangeDirection.FLAT

    def worsened_by_at_least(self, percent: Decimal) -> bool:
        """Whether the metric rose by at least ``percent``.

        Named for cost-like metrics, where an increase is a deterioration.

        Args:
            percent: Threshold as a percentage, e.g. ``Decimal(25)``.

        Returns:
            ``True`` when the increase meets or exceeds the threshold.
        """
        change = self.percent_change
        return change is not None and change >= percent

    def declined_by_at_least(self, percent: Decimal) -> bool:
        """Whether the metric fell by at least ``percent``.

        Named for quality metrics such as click-through rate, where a fall is a
        deterioration.

        Args:
            percent: Threshold as a positive percentage, e.g. ``Decimal(25)``.

        Returns:
            ``True`` when the decline meets or exceeds the threshold.
        """
        change = self.percent_change
        return change is not None and change <= -percent


@dataclass(frozen=True, slots=True)
class PeriodComparison:
    """A current period's metrics set against the period immediately before it.

    Attributes:
        current: Metrics for the period under examination.
        previous: Metrics for the equally long period directly preceding it.
    """

    current: PerformanceMetrics
    previous: PerformanceMetrics

    @property
    def spend(self) -> MetricChange:
        """Movement in total spend."""
        return MetricChange(current=self.current.spend, previous=self.previous.spend)

    @property
    def cost_per_acquisition(self) -> MetricChange:
        """Movement in cost per conversion."""
        return MetricChange(
            current=self.current.cost_per_acquisition,
            previous=self.previous.cost_per_acquisition,
        )

    @property
    def click_through_rate(self) -> MetricChange:
        """Movement in click-through rate."""
        return MetricChange(
            current=self.current.click_through_rate,
            previous=self.previous.click_through_rate,
        )

    @property
    def cost_per_mille(self) -> MetricChange:
        """Movement in cost per thousand impressions."""
        return MetricChange(
            current=self.current.cost_per_mille,
            previous=self.previous.cost_per_mille,
        )

    @property
    def return_on_ad_spend(self) -> MetricChange:
        """Movement in return on ad spend."""
        return MetricChange(
            current=self.current.return_on_ad_spend,
            previous=self.previous.return_on_ad_spend,
        )

    @property
    def frequency(self) -> MetricChange:
        """Movement in average impressions per person."""
        return MetricChange(current=self.current.frequency, previous=self.previous.frequency)

    @property
    def conversions(self) -> MetricChange:
        """Movement in conversion count."""
        return MetricChange(
            current=Decimal(self.current.conversions),
            previous=Decimal(self.previous.conversions),
        )


def split_window(
    rows: Sequence[MetricSource],
    row_dates: Sequence[date],
    *,
    boundary: date,
) -> tuple[list[MetricSource], list[MetricSource]]:
    """Partition rows into the periods before and from a boundary date.

    Args:
        rows: Rows to partition, parallel to ``row_dates``.
        row_dates: The date each row belongs to, in the same order as ``rows``.
        boundary: First day of the current period. Rows dated earlier belong to
            the previous period.

    Returns:
        A ``(previous, current)`` pair of row lists.

    Raises:
        ValueError: If ``rows`` and ``row_dates`` are not the same length, which
            would silently misattribute rows to the wrong period.
    """
    if len(rows) != len(row_dates):
        message = (
            f"rows and row_dates must be the same length; "
            f"got {len(rows)} rows and {len(row_dates)} dates"
        )
        raise ValueError(message)

    previous: list[MetricSource] = []
    current: list[MetricSource] = []
    for row, row_date in zip(rows, row_dates, strict=True):
        if row_date < boundary:
            previous.append(row)
        else:
            current.append(row)
    return previous, current


def previous_period_bounds(*, since: date, until: date) -> tuple[date, date]:
    """Return the equally long period immediately preceding ``since``.

    Equal length matters: comparing a 7-day window against a 30-day one would
    make every metric look like it collapsed.

    Args:
        since: First day of the current period, inclusive.
        until: Last day of the current period, inclusive.

    Returns:
        The ``(since, until)`` bounds of the preceding period.

    Raises:
        ValueError: If ``until`` precedes ``since``.
    """
    if until < since:
        message = f"until ({until}) must not precede since ({since})"
        raise ValueError(message)

    day_count = (until - since).days + 1
    previous_until = since - timedelta(days=1)
    previous_since = previous_until - timedelta(days=day_count - 1)
    return previous_since, previous_until
