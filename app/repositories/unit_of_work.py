"""Groups the repositories that share a single transaction.

A synchronization writes accounts, campaigns, ad sets, ads, creatives, and
insight rows. Those writes describe one consistent snapshot of an advertiser's
account, and a failure partway through must leave the database describing the
state before the sync rather than a state that never existed. That requires one
transaction spanning all six repositories, which requires them to share one
session — which is what this class is for.

It also gives services a single injected dependency instead of six. A service
constructor taking six repositories is a constructor nobody can call in a test
without building six objects; taking one factory is a constructor a test can
satisfy with one stub.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.orm import Session, sessionmaker

from app.database.session import session_scope
from app.repositories.ad_account_repository import AdAccountRepository
from app.repositories.ad_creative_repository import AdCreativeRepository
from app.repositories.ad_repository import AdRepository
from app.repositories.ad_set_repository import AdSetRepository
from app.repositories.campaign_repository import CampaignRepository
from app.repositories.insight_repository import InsightRepository
from app.repositories.recommendation_repository import RecommendationRepository


class UnitOfWork:
    """The repositories bound to one session, and therefore one transaction.

    Attributes are plain instances rather than lazily constructed properties:
    repository construction is a single object allocation, so deferring it would
    add indirection to save nothing.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self.ad_accounts = AdAccountRepository(session)
        self.campaigns = CampaignRepository(session)
        self.ad_sets = AdSetRepository(session)
        self.ads = AdRepository(session)
        self.creatives = AdCreativeRepository(session)
        self.insights = InsightRepository(session)
        self.recommendations = RecommendationRepository(session)

    @property
    def session(self) -> Session:
        """The session every repository here writes through.

        Exposed for the migration and test fixtures that need to reach the
        session directly. Services use the repositories.
        """
        return self._session


class UnitOfWorkFactory:
    """Opens transactions and hands out the repositories bound to them.

    Injected into services so that a service owns *when* a transaction begins
    and ends without owning *how* a session is built.
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    @contextmanager
    def start(self) -> Iterator[UnitOfWork]:
        """Open a transaction and yield the repositories bound to it.

        Commits when the block completes, rolls back when it raises.

        Yields:
            A unit of work whose repositories share one transaction.

        Raises:
            DatabaseError: If the transaction fails to commit.
        """
        with session_scope(self._session_factory) as session:
            yield UnitOfWork(session)
