"""Business logic for reading ad creatives."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.models.ad_creative import AdCreative
from app.repositories.unit_of_work import UnitOfWorkFactory
from app.services.sync_service import SyncService

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CreativeUsage:
    """A creative together with how widely it is deployed.

    The usage count is what makes a creative listing actionable. A creative used
    by thirty ads is a template whose failure is systemic; one used by a single
    ad is a local problem. The listing shows both so the difference is visible
    without a second query.

    Attributes:
        creative: The stored creative.
        ad_count: How many ads reference it.
        active_ad_count: How many of those ads are configured to deliver.
    """

    creative: AdCreative
    ad_count: int
    active_ad_count: int

    @property
    def is_in_use(self) -> bool:
        """Whether any delivering ad currently references this creative."""
        return self.active_ad_count > 0


class CreativeService:
    """Answers questions about an account's creative library."""

    def __init__(
        self,
        *,
        unit_of_work_factory: UnitOfWorkFactory,
        sync_service: SyncService,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._sync_service = sync_service

    def list_creatives(
        self,
        account_remote_id: str,
        *,
        refresh: bool = False,
        in_use_only: bool = False,
    ) -> list[CreativeUsage]:
        """List an account's creatives with their deployment counts.

        Args:
            account_remote_id: Meta account ID, including the ``act_`` prefix.
            refresh: Fetch the account structure from Meta before reading.
            in_use_only: Exclude creatives with no delivering ad. A mature
                account accumulates hundreds of retired creatives that bury the
                ones currently spending money.

        Returns:
            Creatives with usage counts, most widely used first.

        Raises:
            EntityNotFoundError: If the account has not been synchronized.
            MetaApiError: If ``refresh`` is set and a Graph API request fails.
        """
        if refresh:
            self._sync_service.sync_structure(account_remote_id)

        with self._unit_of_work_factory.start() as unit_of_work:
            account = unit_of_work.ad_accounts.require_by_remote_id(account_remote_id)
            creatives = unit_of_work.creatives.list_for_account(account.id)

            usages: list[CreativeUsage] = []
            for creative in creatives:
                ads = unit_of_work.ads.list_using_creative(creative.id)
                usage = CreativeUsage(
                    creative=creative,
                    ad_count=len(ads),
                    active_ad_count=sum(1 for ad in ads if ad.is_delivering),
                )
                if in_use_only and not usage.is_in_use:
                    continue
                usages.append(usage)

        return sorted(
            usages, key=lambda usage: (usage.active_ad_count, usage.ad_count), reverse=True
        )
