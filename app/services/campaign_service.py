"""Business logic for reading campaigns."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from app.models.campaign import Campaign
from app.models.enums import EntityStatus
from app.repositories.unit_of_work import UnitOfWorkFactory
from app.services.sync_service import SyncService

_logger = logging.getLogger(__name__)


class CampaignService:
    """Answers questions about campaigns, refreshing from Meta on request."""

    def __init__(
        self,
        *,
        unit_of_work_factory: UnitOfWorkFactory,
        sync_service: SyncService,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._sync_service = sync_service

    def list_campaigns(
        self,
        account_remote_id: str,
        *,
        statuses: Sequence[EntityStatus] | None = None,
        name_contains: str | None = None,
        refresh: bool = False,
    ) -> list[Campaign]:
        """List an account's campaigns.

        Args:
            account_remote_id: Meta account ID, including the ``act_`` prefix.
            statuses: Restrict to these configured statuses. ``None`` means all.
            name_contains: Case-insensitive substring match on the name.
            refresh: Fetch the account structure from Meta before reading.

        Returns:
            Matching campaigns, most recently created first.

        Raises:
            EntityNotFoundError: If the account has not been synchronized.
            MetaApiError: If ``refresh`` is set and a Graph API request fails.
        """
        if refresh:
            self._sync_service.sync_structure(account_remote_id)

        with self._unit_of_work_factory.start() as unit_of_work:
            account = unit_of_work.ad_accounts.require_by_remote_id(account_remote_id)
            return unit_of_work.campaigns.list_for_account(
                account.id,
                statuses=statuses,
                name_contains=name_contains,
            )

    def count_campaigns(self, account_remote_id: str) -> int:
        """Count an account's stored campaigns.

        Args:
            account_remote_id: Meta account ID, including the ``act_`` prefix.

        Returns:
            The number of stored campaigns.

        Raises:
            EntityNotFoundError: If the account has not been synchronized.
        """
        with self._unit_of_work_factory.start() as unit_of_work:
            account = unit_of_work.ad_accounts.require_by_remote_id(account_remote_id)
            return unit_of_work.campaigns.count_for_account(account.id)
