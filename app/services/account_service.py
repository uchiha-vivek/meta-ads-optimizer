"""Business logic for reading ad accounts."""

from __future__ import annotations

import logging

from app.models.ad_account import AdAccount
from app.repositories.unit_of_work import UnitOfWorkFactory
from app.services.sync_service import SyncService

_logger = logging.getLogger(__name__)


class AccountService:
    """Answers questions about ad accounts, refreshing from Meta on request.

    Reads are served from the database rather than from the API. A command that
    hit Meta on every invocation would be slow, would consume rate limit budget
    to display data that has not changed, and would fail entirely when the API
    is unavailable. Refreshing is therefore explicit and opt-in.
    """

    def __init__(
        self,
        *,
        unit_of_work_factory: UnitOfWorkFactory,
        sync_service: SyncService,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._sync_service = sync_service

    def list_accounts(self, *, refresh: bool = False) -> list[AdAccount]:
        """List the known ad accounts.

        Args:
            refresh: Fetch from Meta before reading. Required on first use,
                since nothing is stored until something has been synchronized.

        Returns:
            Every stored account, ordered by name.

        Raises:
            MetaApiError: If ``refresh`` is set and the Graph API request fails.
        """
        if refresh:
            self._sync_service.sync_accounts()

        with self._unit_of_work_factory.start() as unit_of_work:
            return unit_of_work.ad_accounts.list_all()

    def get_account(self, account_remote_id: str, *, refresh: bool = False) -> AdAccount:
        """Fetch one account by its Meta ID.

        Args:
            account_remote_id: Meta account ID, including the ``act_`` prefix.
            refresh: Fetch from Meta before reading.

        Returns:
            The account.

        Raises:
            EntityNotFoundError: If the account has not been synchronized.
            MetaApiError: If ``refresh`` is set and the Graph API request fails.
        """
        if refresh:
            self._sync_service.sync_accounts()

        with self._unit_of_work_factory.start() as unit_of_work:
            return unit_of_work.ad_accounts.require_by_remote_id(account_remote_id)
