"""Persistence for :class:`~app.models.ad_creative.AdCreative`."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.ad_creative import AdCreative
from app.repositories.base import EntityStore, copy_scalar_columns


class AdCreativeRepository:
    """Reads and writes ad creatives."""

    def __init__(self, session: Session) -> None:
        self._store: EntityStore[AdCreative] = EntityStore(session, AdCreative)

    def get_by_remote_id(self, remote_id: str) -> AdCreative | None:
        """Find a creative by its Meta ID.

        Args:
            remote_id: Meta creative ID.

        Returns:
            The creative, or ``None`` when it has never been synchronized.
        """
        statement = select(AdCreative).where(AdCreative.remote_id == remote_id)
        return self._store.find_one(statement)

    def list_for_account(self, ad_account_id: int) -> list[AdCreative]:
        """List the creatives in an account's library.

        Args:
            ad_account_id: Local primary key of the owning account.

        Returns:
            Matching creatives, ordered by name.
        """
        statement = (
            select(AdCreative)
            .where(AdCreative.ad_account_id == ad_account_id)
            .order_by(AdCreative.name, AdCreative.id)
        )
        return self._store.find_all(statement)

    def upsert(self, incoming: AdCreative) -> AdCreative:
        """Insert ``incoming``, or refresh the stored row with its values.

        Args:
            incoming: Transient creative built from a Graph API response.

        Returns:
            The persistent instance, whether newly inserted or updated.
        """
        existing = self.get_by_remote_id(incoming.remote_id)
        if existing is None:
            return self._store.add(incoming)
        copy_scalar_columns(source=incoming, target=existing)
        self._store.flush()
        return existing
