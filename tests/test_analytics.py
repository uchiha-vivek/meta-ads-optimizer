"""Tests for metric derivation, trend comparison, and per-entity aggregation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pytest

from app.analytics.aggregation import summarize_by_entity
from app.analytics.metrics import PerformanceMetrics, safe_divide
from app.analytics.trends import (
    ChangeDirection,
    MetricChange,
    PeriodComparison,
    previous_period_bounds,
    split_window,
)


@dataclass(frozen=True)
class FakeRow:
    """A stand-in for an insight row, satisfying the analytics protocols."""

    entity_remote_id: str
    entity_name: str | None
    date_start: date
    spend: Decimal = Decimal(0)
    impressions: int = 0
    clicks: int = 0
    reach: int = 0
    conversions: int = 0
    conversion_value: Decimal = Decimal(0)


# ---------------------------------------------------------------------------
# safe_divide and derived metrics
# ---------------------------------------------------------------------------


def test_safe_divide_returns_none_rather_than_raising() -> None:
    assert safe_divide(Decimal(10), Decimal(0)) is None
    assert safe_divide(Decimal(10), Decimal(4)) == Decimal("2.5")


def test_derived_metrics_are_computed_from_measures() -> None:
    metrics = PerformanceMetrics(
        spend=Decimal("100.00"),
        impressions=10_000,
        clicks=200,
        reach=4_000,
        conversions=10,
        conversion_value=Decimal("500.00"),
        day_count=5,
    )

    assert metrics.click_through_rate == Decimal(2)
    assert metrics.cost_per_click == Decimal("0.5")
    assert metrics.cost_per_mille == Decimal(10)
    assert metrics.cost_per_acquisition == Decimal(10)
    assert metrics.return_on_ad_spend == Decimal(5)
    assert metrics.frequency == Decimal("2.5")
    assert metrics.conversion_rate == Decimal(5)
    assert metrics.average_daily_spend == Decimal(20)


def test_undefined_metrics_are_none_not_zero() -> None:
    # Zero would sort as "cheapest" and silently corrupt any ranking.
    empty = PerformanceMetrics()

    assert empty.click_through_rate is None
    assert empty.cost_per_click is None
    assert empty.cost_per_acquisition is None
    assert empty.return_on_ad_spend is None
    assert empty.frequency is None
    assert empty.average_daily_spend is None
    assert empty.has_delivery is False


def test_spend_without_conversions_has_undefined_cost_per_acquisition() -> None:
    metrics = PerformanceMetrics(spend=Decimal(500), impressions=1_000, conversions=0)

    assert metrics.cost_per_acquisition is None


def test_aggregate_click_through_rate_is_impression_weighted() -> None:
    """The central correctness claim: derive after summing, never average.

    One day drew 1 click from 10 impressions (10%), another 10 clicks from
    10,000 (0.1%). The mean of those rates is about 5.05%, which describes
    neither day. The impression-weighted answer is 11/10010, near 0.11%.
    """
    rows = [
        FakeRow("c1", "Campaign", date(2026, 6, 1), impressions=10, clicks=1),
        FakeRow("c1", "Campaign", date(2026, 6, 2), impressions=10_000, clicks=10),
    ]

    aggregated = PerformanceMetrics.from_sources(rows)

    assert aggregated.impressions == 10_010
    assert aggregated.clicks == 11
    ctr = aggregated.click_through_rate
    assert ctr is not None
    assert ctr < Decimal("0.2")


def test_from_sources_sums_every_measure() -> None:
    rows = [
        FakeRow(
            "c1",
            "Campaign",
            date(2026, 6, 1),
            spend=Decimal("10.50"),
            impressions=100,
            clicks=5,
            reach=80,
            conversions=1,
            conversion_value=Decimal("40.00"),
        ),
        FakeRow(
            "c1",
            "Campaign",
            date(2026, 6, 2),
            spend=Decimal("20.25"),
            impressions=200,
            clicks=9,
            reach=150,
            conversions=2,
            conversion_value=Decimal("60.00"),
        ),
    ]

    aggregated = PerformanceMetrics.from_sources(rows, day_count=2)

    assert aggregated.spend == Decimal("30.75")
    assert aggregated.impressions == 300
    assert aggregated.clicks == 14
    assert aggregated.reach == 230
    assert aggregated.conversions == 3
    assert aggregated.conversion_value == Decimal("100.00")


def test_snapshot_is_json_safe_and_keeps_decimals_exact() -> None:
    metrics = PerformanceMetrics(spend=Decimal("0.10"), impressions=1_000, clicks=10)

    snapshot = metrics.as_snapshot()

    # Strings, not floats: a stored record of what was measured must not acquire
    # binary rounding error on its way into the database.
    assert snapshot["spend"] == "0.10"
    assert snapshot["cpa"] is None
    assert snapshot["impressions"] == 1_000


# ---------------------------------------------------------------------------
# Trends
# ---------------------------------------------------------------------------


def test_percent_change_is_relative_to_the_previous_value() -> None:
    change = MetricChange(current=Decimal(150), previous=Decimal(100))

    assert change.absolute_change == Decimal(50)
    assert change.percent_change == Decimal(50)
    assert change.direction is ChangeDirection.UP


def test_change_from_zero_is_undefined_not_infinite() -> None:
    # Otherwise every campaign's first week reports infinite growth and every
    # trend rule fires on it.
    change = MetricChange(current=Decimal(150), previous=Decimal(0))

    assert change.percent_change is None


def test_change_is_undefined_when_either_side_is_missing() -> None:
    assert MetricChange(current=None, previous=Decimal(10)).percent_change is None
    assert MetricChange(current=Decimal(10), previous=None).percent_change is None
    assert MetricChange(current=None, previous=None).direction is ChangeDirection.UNDEFINED


def test_worsened_and_declined_thresholds() -> None:
    risen = MetricChange(current=Decimal(130), previous=Decimal(100))
    fallen = MetricChange(current=Decimal(70), previous=Decimal(100))

    assert risen.worsened_by_at_least(Decimal(25)) is True
    assert risen.worsened_by_at_least(Decimal(50)) is False
    assert fallen.declined_by_at_least(Decimal(25)) is True
    assert fallen.declined_by_at_least(Decimal(50)) is False


def test_period_comparison_exposes_metric_movements() -> None:
    comparison = PeriodComparison(
        current=PerformanceMetrics(spend=Decimal(200), conversions=10),
        previous=PerformanceMetrics(spend=Decimal(100), conversions=10),
    )

    assert comparison.spend.percent_change == Decimal(100)
    assert comparison.cost_per_acquisition.percent_change == Decimal(100)


def test_previous_period_has_the_same_length_and_ends_the_day_before() -> None:
    since, until = previous_period_bounds(since=date(2026, 6, 8), until=date(2026, 6, 14))

    assert until == date(2026, 6, 7)
    assert since == date(2026, 6, 1)
    assert (until - since).days == 6


def test_previous_period_bounds_rejects_an_inverted_window() -> None:
    with pytest.raises(ValueError, match="must not precede"):
        previous_period_bounds(since=date(2026, 6, 14), until=date(2026, 6, 8))


def test_split_window_rejects_mismatched_lengths() -> None:
    # Silently zipping would misattribute rows to the wrong period.
    with pytest.raises(ValueError, match="same length"):
        split_window([], [date(2026, 6, 1)], boundary=date(2026, 6, 1))


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def test_summarize_splits_entities_into_current_and_previous_windows() -> None:
    rows = [
        FakeRow("c1", "Alpha", date(2026, 6, 1), spend=Decimal(10), impressions=100),
        FakeRow("c1", "Alpha", date(2026, 6, 9), spend=Decimal(30), impressions=300),
        FakeRow("c2", "Beta", date(2026, 6, 10), spend=Decimal(50), impressions=500),
    ]

    summaries = summarize_by_entity(
        rows,
        current_since=date(2026, 6, 8),
        current_until=date(2026, 6, 14),
    )
    by_id = {summary.entity_remote_id: summary for summary in summaries}

    assert by_id["c1"].current.spend == Decimal(30)
    assert by_id["c1"].previous.spend == Decimal(10)
    assert by_id["c1"].has_history is True
    assert by_id["c1"].comparison is not None

    assert by_id["c2"].current.spend == Decimal(50)
    assert by_id["c2"].has_history is False
    # No prior delivery means no baseline, so trend rules must abstain.
    assert by_id["c2"].comparison is None


def test_summaries_are_ordered_by_current_spend_descending() -> None:
    rows = [
        FakeRow("small", "Small", date(2026, 6, 10), spend=Decimal(5), impressions=1),
        FakeRow("large", "Large", date(2026, 6, 10), spend=Decimal(500), impressions=1),
    ]

    summaries = summarize_by_entity(
        rows,
        current_since=date(2026, 6, 8),
        current_until=date(2026, 6, 14),
    )

    assert [summary.entity_remote_id for summary in summaries] == ["large", "small"]


def test_rows_after_the_window_are_excluded() -> None:
    rows = [
        FakeRow("c1", "Alpha", date(2026, 6, 10), spend=Decimal(10), impressions=100),
        FakeRow("c1", "Alpha", date(2026, 6, 20), spend=Decimal(999), impressions=100),
    ]

    summaries = summarize_by_entity(
        rows,
        current_since=date(2026, 6, 8),
        current_until=date(2026, 6, 14),
    )

    # An over-fetching caller must not silently get a longer period than asked.
    assert summaries[0].current.spend == Decimal(10)


def test_summarize_rejects_an_inverted_window() -> None:
    with pytest.raises(ValueError, match="must not precede"):
        summarize_by_entity([], current_since=date(2026, 6, 14), current_until=date(2026, 6, 8))
