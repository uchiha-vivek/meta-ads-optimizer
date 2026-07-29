"""Persistence for :class:`~app.models.ad_set.AdSet`."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.ad_set import AdSet
from app.models.campaign import Campaign
from app.models.enums import EntityStatus
from app.repositories.base import EntityStore, copy_scalar_columns


class AdSetRepository:
    """Reads and writes ad sets.

    Ad sets are usually reached through their campaign, but budget optimization
    needs every ad set in an account at once. That query joins through
    ``campaigns`` rather than denormalizing an account ID onto the ad set,
    because a duplicated foreign key is a second thing that can disagree with
    the first.
    """

    def __init__(self, session: Session) -> None:
        self._store: EntityStore[AdSet] = EntityStore(session, AdSet)

    def get_by_remote_id(self, remote_id: str) -> AdSet | None:
        """Find an ad set by its Meta ID.

        Args:
            remote_id: Meta ad set ID.

        Returns:
            The ad set, or ``None`` when it has never been synchronized.
        """
        statement = select(AdSet).where(AdSet.remote_id == remote_id)
        return self._store.find_one(statement)

    def list_for_campaign(
        self,
        campaign_id: int,
        *,
        statuses: Sequence[EntityStatus] | None = None,
    ) -> list[AdSet]:
        """List the ad sets belonging to one campaign.

        Args:
            campaign_id: Local primary key of the owning campaign.
            statuses: Restrict to these configured statuses. ``None`` means all.

        Returns:
            Matching ad sets, ordered by name.
        """
        statement = select(AdSet).where(AdSet.campaign_id == campaign_id)
        if statuses:
            statement = statement.where(AdSet.status.in_(list(statuses)))
        statement = statement.order_by(AdSet.name, AdSet.id)
        return self._store.find_all(statement)

    def list_for_account(
        self,
        ad_account_id: int,
        *,
        statuses: Sequence[EntityStatus] | None = None,
    ) -> list[AdSet]:
        """List every ad set in an account, joining through its campaigns.

        Args:
            ad_account_id: Local primary key of the owning account.
            statuses: Restrict to these configured statuses. ``None`` means all.

        Returns:
            Matching ad sets, ordered by name.
        """
        statement = (
            select(AdSet)
            .join(Campaign, AdSet.campaign_id == Campaign.id)
            .where(Campaign.ad_account_id == ad_account_id)
        )
        if statuses:
            statement = statement.where(AdSet.status.in_(list(statuses)))
        statement = statement.order_by(AdSet.name, AdSet.id)
        return self._store.find_all(statement)

    def upsert(self, incoming: AdSet) -> AdSet:
        """Insert ``incoming``, or refresh the stored row with its values.

        Args:
            incoming: Transient ad set built from a Graph API response.

        Returns:
            The persistent instance, whether newly inserted or updated.
        """
        existing = self.get_by_remote_id(incoming.remote_id)
        if existing is None:
            return self._store.add(incoming)
        copy_scalar_columns(source=incoming, target=existing)
        self._store.flush()
        return existing
