"""Persistence for :class:`~app.models.campaign.Campaign`."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.campaign import Campaign
from app.models.enums import EntityStatus
from app.repositories.base import EntityStore, copy_scalar_columns


class CampaignRepository:
    """Reads and writes campaigns."""

    def __init__(self, session: Session) -> None:
        self._store: EntityStore[Campaign] = EntityStore(session, Campaign)

    def get_by_remote_id(self, remote_id: str) -> Campaign | None:
        """Find a campaign by its Meta ID.

        Args:
            remote_id: Meta campaign ID.

        Returns:
            The campaign, or ``None`` when it has never been synchronized.
        """
        statement = select(Campaign).where(Campaign.remote_id == remote_id)
        return self._store.find_one(statement)

    def list_for_account(
        self,
        ad_account_id: int,
        *,
        statuses: Sequence[EntityStatus] | None = None,
        name_contains: str | None = None,
    ) -> list[Campaign]:
        """List an account's campaigns, newest first.

        Filtering happens in SQL rather than in the caller, because an account
        can hold thousands of archived campaigns and loading them all in order
        to discard most is wasted work on every command.

        Args:
            ad_account_id: Local primary key of the owning account.
            statuses: Restrict to these configured statuses. ``None`` means all.
            name_contains: Case-insensitive substring match on the name.

        Returns:
            Matching campaigns, most recently created first.
        """
        statement = select(Campaign).where(Campaign.ad_account_id == ad_account_id)
        if statuses:
            statement = statement.where(Campaign.status.in_(list(statuses)))
        if name_contains:
            statement = statement.where(Campaign.name.ilike(f"%{name_contains}%"))
        statement = statement.order_by(Campaign.created_time.desc().nullslast(), Campaign.id.desc())
        return self._store.find_all(statement)

    def list_delivering_for_account(self, ad_account_id: int) -> list[Campaign]:
        """List the account's campaigns configured to spend.

        Args:
            ad_account_id: Local primary key of the owning account.

        Returns:
            Campaigns whose configured status is ``ACTIVE``.
        """
        return self.list_for_account(ad_account_id, statuses=[EntityStatus.ACTIVE])

    def upsert(self, incoming: Campaign) -> Campaign:
        """Insert ``incoming``, or refresh the stored row with its values.

        Args:
            incoming: Transient campaign built from a Graph API response.

        Returns:
            The persistent instance, whether newly inserted or updated.
        """
        existing = self.get_by_remote_id(incoming.remote_id)
        if existing is None:
            return self._store.add(incoming)
        copy_scalar_columns(source=incoming, target=existing)
        self._store.flush()
        return existing

    def count_for_account(self, ad_account_id: int) -> int:
        """Count the campaigns belonging to one account.

        Args:
            ad_account_id: Local primary key of the owning account.

        Returns:
            The number of stored campaigns for that account.
        """
        statement = (
            select(func.count())
            .select_from(Campaign)
            .where(Campaign.ad_account_id == ad_account_id)
        )
        return self._store.scalar_count(statement)
