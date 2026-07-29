"""Persistence for :class:`~app.models.ad_account.AdAccount`."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.ad_account import AdAccount
from app.repositories.base import EntityStore, copy_scalar_columns
from app.utils.exceptions import EntityNotFoundError


class AdAccountRepository:
    """Reads and writes ad accounts.

    The account is the root of every other entity, so almost every service
    begins by resolving a Meta account ID to a local row. That lookup is by
    :attr:`~app.models.ad_account.AdAccount.remote_id`, which is indexed and
    unique.
    """

    def __init__(self, session: Session) -> None:
        self._store: EntityStore[AdAccount] = EntityStore(session, AdAccount)

    def get_by_remote_id(self, remote_id: str) -> AdAccount | None:
        """Find an account by its Meta ID.

        Args:
            remote_id: Meta account ID, including the ``act_`` prefix.

        Returns:
            The account, or ``None`` when it has never been synchronized.
        """
        statement = select(AdAccount).where(AdAccount.remote_id == remote_id)
        return self._store.find_one(statement)

    def require_by_remote_id(self, remote_id: str) -> AdAccount:
        """Find an account by its Meta ID, treating absence as an error.

        Args:
            remote_id: Meta account ID, including the ``act_`` prefix.

        Returns:
            The account.

        Raises:
            EntityNotFoundError: If the account has not been synchronized. The
                message names the command that would fix it, because "not found"
                on a first run means "not synced yet", not "does not exist".
        """
        account = self.get_by_remote_id(remote_id)
        if account is None:
            raise EntityNotFoundError(
                "Ad account is not present locally; run `meta accounts --sync` first",
                context={"remote_id": remote_id},
            )
        return account

    def require_by_id(self, ad_account_id: int) -> AdAccount:
        """Find an account by local primary key, treating absence as an error.

        Used where a foreign key is already in hand, such as resolving the
        owning account of a stored recommendation.

        Args:
            ad_account_id: Local primary key.

        Returns:
            The account.

        Raises:
            EntityNotFoundError: If no account has that key.
        """
        return self._store.require_by_id(ad_account_id)

    def list_all(self) -> list[AdAccount]:
        """Return every known account, ordered by name.

        Returns:
            All stored accounts.
        """
        statement = select(AdAccount).order_by(AdAccount.name, AdAccount.remote_id)
        return self._store.find_all(statement)

    def upsert(self, incoming: AdAccount) -> AdAccount:
        """Insert ``incoming``, or refresh the stored row with its values.

        Args:
            incoming: Transient account built from a Graph API response.

        Returns:
            The persistent instance, whether newly inserted or updated.
        """
        existing = self.get_by_remote_id(incoming.remote_id)
        if existing is None:
            return self._store.add(incoming)
        copy_scalar_columns(source=incoming, target=existing)
        self._store.flush()
        return existing

    def count(self) -> int:
        """Return the number of stored accounts."""
        return self._store.count()
