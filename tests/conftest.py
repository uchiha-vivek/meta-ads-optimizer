"""Shared pytest fixtures.

Two decisions shape this file.

*Tests run against real PostgreSQL.* A repository test exists to prove the SQL
works; running it against SQLite proves a different database's SQL works. The
enum check constraints, ``ILIKE``, ``NULLS LAST`` ordering, and ``BIGINT``
columns used here all behave differently or not at all elsewhere.

*Tests run in a separate database.* A session-scoped fixture creates
``<database>_test`` and drops it afterwards, so running the suite never touches
the development data the developer just synchronized.

Isolation is per test: each one runs inside an outer transaction that is rolled
back at teardown. The session factory joins that transaction with
``join_transaction_mode="create_savepoint"``, so a service calling ``commit()``
is genuinely exercised — its commit releases a savepoint — while the outer
rollback still discards everything.
"""

from __future__ import annotations

import itertools
import os
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
import responses
from pydantic import SecretStr
from sqlalchemy import Connection, Engine, create_engine, make_url, text
from sqlalchemy.orm import Session, sessionmaker

import app.models  # noqa: F401  (registers every table on Base.metadata)
from app.api.client import MetaMarketingClient
from app.auth.credentials import MetaCredentials
from app.config.settings import MetaApiSettings
from app.database.base import Base
from app.models.ad import Ad
from app.models.ad_account import AdAccount
from app.models.ad_creative import AdCreative
from app.models.ad_set import AdSet
from app.models.campaign import Campaign
from app.models.enums import EntityStatus, InsightLevel
from app.models.insight import InsightRecord
from app.repositories.unit_of_work import UnitOfWorkFactory

# Base URL every mocked HTTP test registers against. Not a real host, so a test
# that escapes the mock fails loudly instead of reaching Meta.
TEST_API_BASE_URL = "https://graph.test"
TEST_API_VERSION = "v23.0"
TEST_ACCOUNT_ID = "act_1234567890"

_remote_id_counter = itertools.count(1)


def next_remote_id(prefix: str) -> str:
    """Return a unique Meta-style identifier for use in fixtures."""
    return f"{prefix}_{next(_remote_id_counter)}"


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def database_url() -> str:
    """The development database URL, from the environment."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL is not set; run the suite through docker compose")
    return url


@pytest.fixture(scope="session")
def test_database_url(database_url: str) -> Iterator[str]:
    """Create a dedicated test database and drop it when the session ends.

    ``CREATE DATABASE`` cannot run inside a transaction, hence the autocommit
    connection to the maintenance database.
    """
    url = make_url(database_url)
    test_database_name = f"{url.database}_test"
    admin_engine = create_engine(
        url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
    )

    with admin_engine.connect() as connection:
        connection.execute(text(f'DROP DATABASE IF EXISTS "{test_database_name}" WITH (FORCE)'))
        connection.execute(text(f'CREATE DATABASE "{test_database_name}"'))

    yield url.set(database=test_database_name).render_as_string(hide_password=False)

    with admin_engine.connect() as connection:
        connection.execute(text(f'DROP DATABASE IF EXISTS "{test_database_name}" WITH (FORCE)'))
    admin_engine.dispose()


@pytest.fixture(scope="session")
def engine(test_database_url: str) -> Iterator[Engine]:
    """An engine bound to the test database, with the schema created."""
    test_engine = create_engine(test_database_url)
    Base.metadata.create_all(test_engine)
    yield test_engine
    test_engine.dispose()


@pytest.fixture
def connection(engine: Engine) -> Iterator[Connection]:
    """A connection holding an open transaction that is rolled back afterwards."""
    open_connection = engine.connect()
    transaction = open_connection.begin()
    try:
        yield open_connection
    finally:
        transaction.rollback()
        open_connection.close()


@pytest.fixture
def session_factory(connection: Connection) -> sessionmaker[Session]:
    """A session factory that joins the test's outer transaction.

    ``create_savepoint`` is what allows production code to call ``commit()``
    unchanged: the commit releases a savepoint rather than ending the outer
    transaction, so the rollback at teardown still discards every write.
    """
    return sessionmaker(
        bind=connection,
        autoflush=False,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )


@pytest.fixture
def db_session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """A session for tests that drive repositories directly."""
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def unit_of_work_factory(session_factory: sessionmaker[Session]) -> UnitOfWorkFactory:
    """A unit-of-work factory bound to the test transaction."""
    return UnitOfWorkFactory(session_factory)


# ---------------------------------------------------------------------------
# Model builders
#
# Plain functions rather than fixtures so a test can build several variants in
# one body without declaring a fixture per variant.
# ---------------------------------------------------------------------------


def build_account(**overrides: object) -> AdAccount:
    """Build an unsaved ad account with sensible defaults."""
    values: dict[str, object] = {
        "remote_id": next_remote_id("act"),
        "name": "Test Account",
        "business_name": "Test Business",
        "currency": "USD",
        "timezone_name": "America/New_York",
        "account_status": 1,
        "spend_cap": Decimal("10000.00"),
        "amount_spent": Decimal("2500.00"),
    }
    values.update(overrides)
    return AdAccount(**values)


def build_campaign(ad_account_id: int, **overrides: object) -> Campaign:
    """Build an unsaved campaign with sensible defaults."""
    values: dict[str, object] = {
        "remote_id": next_remote_id("camp"),
        "ad_account_id": ad_account_id,
        "name": "Test Campaign",
        "status": EntityStatus.ACTIVE,
        "effective_status": "ACTIVE",
        "objective": "OUTCOME_SALES",
        "daily_budget": Decimal("100.00"),
        "created_time": datetime(2026, 1, 1, tzinfo=UTC),
    }
    values.update(overrides)
    return Campaign(**values)


def build_ad_set(campaign_id: int, **overrides: object) -> AdSet:
    """Build an unsaved ad set with sensible defaults."""
    values: dict[str, object] = {
        "remote_id": next_remote_id("adset"),
        "campaign_id": campaign_id,
        "name": "Test Ad Set",
        "status": EntityStatus.ACTIVE,
        "optimization_goal": "OFFSITE_CONVERSIONS",
        "daily_budget": Decimal("50.00"),
    }
    values.update(overrides)
    return AdSet(**values)


def build_creative(ad_account_id: int, **overrides: object) -> AdCreative:
    """Build an unsaved creative with sensible defaults."""
    values: dict[str, object] = {
        "remote_id": next_remote_id("creative"),
        "ad_account_id": ad_account_id,
        "name": "Test Creative",
        "title": "Buy now",
        "body": "The best product you will ever own.",
        "call_to_action_type": "SHOP_NOW",
        "object_type": "SHARE",
    }
    values.update(overrides)
    return AdCreative(**values)


def build_ad(ad_set_id: int, **overrides: object) -> Ad:
    """Build an unsaved ad with sensible defaults."""
    values: dict[str, object] = {
        "remote_id": next_remote_id("ad"),
        "ad_set_id": ad_set_id,
        "name": "Test Ad",
        "status": EntityStatus.ACTIVE,
    }
    values.update(overrides)
    return Ad(**values)


def build_insight(
    ad_account_id: int,
    *,
    entity_remote_id: str,
    day: date,
    level: InsightLevel = InsightLevel.CAMPAIGN,
    **overrides: object,
) -> InsightRecord:
    """Build an unsaved single-day insight row with sensible defaults."""
    values: dict[str, object] = {
        "ad_account_id": ad_account_id,
        "level": level,
        "entity_remote_id": entity_remote_id,
        "entity_name": "Test Campaign",
        "date_start": day,
        "date_stop": day,
        "spend": Decimal("100.00"),
        "impressions": 10_000,
        "clicks": 200,
        "reach": 5_000,
        "conversions": 10,
        "conversion_value": Decimal("500.00"),
    }
    values.update(overrides)
    return InsightRecord(**values)


@pytest.fixture
def persisted_account(db_session: Session) -> AdAccount:
    """An ad account already saved in the test transaction."""
    account = build_account(remote_id=TEST_ACCOUNT_ID)
    db_session.add(account)
    db_session.flush()
    return account


@pytest.fixture
def today() -> date:
    """A fixed 'today' so date arithmetic in tests is deterministic."""
    return date(2026, 6, 15)


@pytest.fixture
def window(today: date) -> tuple[date, date]:
    """A seven-day reporting window ending yesterday relative to ``today``."""
    until = today - timedelta(days=1)
    return until - timedelta(days=6), until


# ---------------------------------------------------------------------------
# Meta API
# ---------------------------------------------------------------------------


@pytest.fixture
def meta_settings() -> MetaApiSettings:
    """API settings pointed at a non-routable host, with negligible delays.

    Retry delays are microseconds so the full backoff path can be exercised
    without the suite actually waiting.
    """
    return MetaApiSettings(
        access_token=SecretStr("test-access-token"),
        app_id="1234567890",
        app_secret=SecretStr("test-app-secret"),
        ad_account_id=TEST_ACCOUNT_ID,
        api_version=TEST_API_VERSION,
        api_base_url=TEST_API_BASE_URL,
        request_timeout_seconds=1.0,
        max_retries=2,
        retry_backoff_seconds=0.001,
        rate_limit_pause_seconds=0.001,
    )


@pytest.fixture
def credentials(meta_settings: MetaApiSettings) -> MetaCredentials:
    """Credentials built from the test settings."""
    return MetaCredentials.from_settings(meta_settings)


@pytest.fixture
def recorded_sleeps() -> list[float]:
    """Collects every delay the client would have slept for."""
    return []


@pytest.fixture
def sleeper(recorded_sleeps: list[float]) -> Callable[[float], None]:
    """A sleep replacement that records instead of waiting."""
    return recorded_sleeps.append


@pytest.fixture
def mocked_responses() -> Iterator[responses.RequestsMock]:
    """Intercept outbound HTTP, asserting every registered mock was used."""
    with responses.RequestsMock() as mock:
        yield mock


@pytest.fixture
def client(
    meta_settings: MetaApiSettings,
    credentials: MetaCredentials,
    sleeper: Callable[[float], None],
) -> Iterator[MetaMarketingClient]:
    """A Meta client whose clock is a recorder rather than a real sleep."""
    api_client = MetaMarketingClient(
        settings=meta_settings,
        credentials=credentials,
        sleeper=sleeper,
    )
    try:
        yield api_client
    finally:
        api_client.close()


def graph_url(path: str) -> str:
    """Build the full URL the client will request for a Graph API path."""
    return f"{TEST_API_BASE_URL}/{TEST_API_VERSION}/{path}"
