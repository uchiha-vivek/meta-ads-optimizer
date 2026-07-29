"""Grouping of insight rows into per-entity, per-period metrics.

Kept here rather than in a service because two services need it — reporting and
optimization ask the same question and must not answer it differently. It stays
pure by consuming a structural protocol, so it never imports the ORM.

The windowing convention is fixed and deliberate: the *current* period is the
range the caller asked about, and the *previous* period is the equally long
range immediately preceding it. Equal length is what makes the comparison
meaningful.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Protocol, runtime_checkable

from app.analytics.metrics import MetricSource, PerformanceMetrics
from app.analytics.trends import PeriodComparison


@runtime_checkable
class DatedEntityMetrics(MetricSource, Protocol):
    """An insight row that knows which entity and which day it describes."""

    @property
    def entity_remote_id(self) -> str:
        """Meta ID of the measured entity."""
        ...

    @property
    def entity_name(self) -> str | None:
        """Human-readable name of the measured entity, when known."""
        ...

    @property
    def date_start(self) -> date:
        """First day of the row's reporting window."""
        ...


@dataclass(frozen=True, slots=True)
class EntityWindowMetrics:
    """One entity's performance in the current window and the one before it.

    Attributes:
        entity_remote_id: Meta ID of the entity.
        entity_name: Name taken from the most recent row that carried one.
        current: Metrics for the requested window.
        previous: Metrics for the equally long preceding window.
        has_history: Whether the previous window contained any delivery. When
            ``False``, trend comparisons are meaningless and rules that depend
            on them must abstain.
    """

    entity_remote_id: str
    entity_name: str | None
    current: PerformanceMetrics
    previous: PerformanceMetrics
    has_history: bool

    @property
    def comparison(self) -> PeriodComparison | None:
        """The two windows set against each other, or ``None`` without history."""
        if not self.has_history:
            return None
        return PeriodComparison(current=self.current, previous=self.previous)


def summarize_by_entity(
    rows: Iterable[DatedEntityMetrics],
    *,
    current_since: date,
    current_until: date,
) -> list[EntityWindowMetrics]:
    """Group rows by entity and split each group into current and prior windows.

    Rows dated on or after ``current_since`` form the current window; earlier
    rows form the previous one. Rows after ``current_until`` are ignored rather
    than folded into the current window, so a caller that over-fetched does not
    silently get a longer period than it asked for.

    Args:
        rows: Insight rows spanning both windows.
        current_since: First day of the current window, inclusive.
        current_until: Last day of the current window, inclusive.

    Returns:
        One entry per entity, ordered by current spend descending — the order an
        advertiser reads, since the largest spender is where attention belongs.

    Raises:
        ValueError: If ``current_until`` precedes ``current_since``.
    """
    if current_until < current_since:
        message = (
            f"current_until ({current_until}) must not precede current_since ({current_since})"
        )
        raise ValueError(message)

    current_rows: dict[str, list[DatedEntityMetrics]] = {}
    previous_rows: dict[str, list[DatedEntityMetrics]] = {}
    names: dict[str, str | None] = {}

    for row in rows:
        entity_id = row.entity_remote_id
        if row.entity_name:
            names[entity_id] = row.entity_name
        names.setdefault(entity_id, None)

        if row.date_start > current_until:
            continue
        bucket = current_rows if row.date_start >= current_since else previous_rows
        bucket.setdefault(entity_id, []).append(row)

    current_day_count = (current_until - current_since).days + 1

    summaries: list[EntityWindowMetrics] = []
    for entity_id in sorted({*current_rows, *previous_rows}):
        current_group: Sequence[DatedEntityMetrics] = current_rows.get(entity_id, [])
        previous_group: Sequence[DatedEntityMetrics] = previous_rows.get(entity_id, [])

        current_metrics = PerformanceMetrics.from_sources(
            current_group,
            day_count=current_day_count,
        )
        previous_metrics = PerformanceMetrics.from_sources(
            previous_group,
            day_count=current_day_count,
        )
        summaries.append(
            EntityWindowMetrics(
                entity_remote_id=entity_id,
                entity_name=names.get(entity_id),
                current=current_metrics,
                previous=previous_metrics,
                has_history=previous_metrics.has_delivery,
            )
        )

    return sorted(summaries, key=lambda summary: summary.current.spend, reverse=True)
