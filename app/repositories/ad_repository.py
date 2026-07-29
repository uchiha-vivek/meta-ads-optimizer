"""Persistence for :class:`~app.models.ad.Ad`."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.ad import Ad
from app.models.ad_set import AdSet
from app.models.campaign import Campaign
from app.repositories.base import EntityStore, copy_scalar_columns


class AdRepository:
    """Reads and writes ads."""

    def __init__(self, session: Session) -> None:
        self._store: EntityStore[Ad] = EntityStore(session, Ad)

    def get_by_remote_id(self, remote_id: str) -> Ad | None:
        """Find an ad by its Meta ID.

        Args:
            remote_id: Meta ad ID.

        Returns:
            The ad, or ``None`` when it has never been synchronized.
        """
        statement = select(Ad).where(Ad.remote_id == remote_id)
        return self._store.find_one(statement)

    def list_for_ad_set(self, ad_set_id: int) -> list[Ad]:
        """List the ads belonging to one ad set.

        Args:
            ad_set_id: Local primary key of the owning ad set.

        Returns:
            Matching ads, ordered by name.
        """
        statement = select(Ad).where(Ad.ad_set_id == ad_set_id).order_by(Ad.name, Ad.id)
        return self._store.find_all(statement)

    def list_for_account(self, ad_account_id: int) -> list[Ad]:
        """List every ad in an account, joining through ad sets and campaigns.

        Args:
            ad_account_id: Local primary key of the owning account.

        Returns:
            Matching ads, ordered by name.
        """
        statement = (
            select(Ad)
            .join(AdSet, Ad.ad_set_id == AdSet.id)
            .join(Campaign, AdSet.campaign_id == Campaign.id)
            .where(Campaign.ad_account_id == ad_account_id)
            .order_by(Ad.name, Ad.id)
        )
        return self._store.find_all(statement)

    def list_using_creative(self, creative_id: int) -> list[Ad]:
        """List the ads referencing one creative.

        Answers the question the creative fatigue rule asks: is this creative
        failing everywhere, or only in one audience?

        Args:
            creative_id: Local primary key of the creative.

        Returns:
            Ads referencing that creative, ordered by name.
        """
        statement = select(Ad).where(Ad.creative_id == creative_id).order_by(Ad.name, Ad.id)
        return self._store.find_all(statement)

    def upsert(self, incoming: Ad) -> Ad:
        """Insert ``incoming``, or refresh the stored row with its values.

        Args:
            incoming: Transient ad built from a Graph API response.

        Returns:
            The persistent instance, whether newly inserted or updated.
        """
        existing = self.get_by_remote_id(incoming.remote_id)
        if existing is None:
            return self._store.add(incoming)
        copy_scalar_columns(source=incoming, target=existing)
        self._store.flush()
        return existing
