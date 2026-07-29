"""Business logic for reading and reporting performance insights."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from app.analytics.aggregation import EntityWindowMetrics, summarize_by_entity
from app.analytics.metrics import PerformanceMetrics
from app.analytics.trends import previous_period_bounds
from app.models.enums import InsightLevel
from app.repositories.unit_of_work import UnitOfWorkFactory
from app.services.sync_service import SyncService

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PerformanceReport:
    """Per-entity performance for a window, with the account's context.

    Carries the currency because every monetary figure in ``entries`` is
    meaningless without it, and the caller rendering a table should not have to
    issue a second query to find out what unit it is displaying.

    Attributes:
        account_remote_id: Meta ID of the account reported on.
        currency: ISO 4217 code the figures are expressed in.
        level: Aggregation level the entries describe.
        since: First day of the reported window, inclusive.
        until: Last day of the reported window, inclusive.
        entries: One entry per entity, highest current spend first.
    """

    account_remote_id: str
    currency: str | None
    level: InsightLevel
    since: date
    until: date
    entries: list[EntityWindowMetrics]

    @property
    def totals(self) -> PerformanceMetrics:
        """Account-wide totals for the current window.

        Summed from the same rows the entries were built from, so the total
        cannot disagree with the sum of what is displayed beneath it.
        """
        return PerformanceMetrics.from_sources(
            [entry.current for entry in self.entries],
            day_count=(self.until - self.since).days + 1,
        )


class InsightService:
    """Turns stored insight rows into per-entity performance reports."""

    def __init__(
        self,
        *,
        unit_of_work_factory: UnitOfWorkFactory,
        sync_service: SyncService,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._sync_service = sync_service

    def performance_report(
        self,
        account_remote_id: str,
        *,
        level: InsightLevel,
        since: date,
        until: date,
        refresh: bool = False,
    ) -> PerformanceReport:
        """Build a per-entity performance report for one window.

        Rows are read for the requested window *and* the equally long window
        before it, so that every entry carries a comparison. Fetching both in
        one query is why trend rules cost no extra database round trips.

        Args:
            account_remote_id: Meta account ID, including the ``act_`` prefix.
            level: Aggregation level to report on.
            since: First day of the window, inclusive.
            until: Last day of the window, inclusive.
            refresh: Fetch insights from Meta before reading. Both windows are
                fetched, since a comparison against absent history is no
                comparison at all.

        Returns:
            The report, entries ordered by current spend descending.

        Raises:
            EntityNotFoundError: If the account has not been synchronized.
            ValueError: If ``until`` precedes ``since``.
            MetaApiError: If ``refresh`` is set and a Graph API request fails.
        """
        previous_since, _ = previous_period_bounds(since=since, until=until)

        if refresh:
            self._sync_service.sync_insights(
                account_remote_id,
                level=level,
                since=previous_since,
                until=until,
            )

        with self._unit_of_work_factory.start() as unit_of_work:
            account = unit_of_work.ad_accounts.require_by_remote_id(account_remote_id)
            rows = unit_of_work.insights.list_for_account(
                account.id,
                level=level,
                since=previous_since,
                until=until,
            )
            entries = summarize_by_entity(rows, current_since=since, current_until=until)
            currency = account.currency

        _logger.debug(
            "Built performance report",
            extra={
                "account_remote_id": account_remote_id,
                "level": level.value,
                "entities": len(entries),
            },
        )
        return PerformanceReport(
            account_remote_id=account_remote_id,
            currency=currency,
            level=level,
            since=since,
            until=until,
            entries=entries,
        )
