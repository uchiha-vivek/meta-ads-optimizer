"""Tests for the service layer.

Services are exercised against the real database with a mocked Meta client. That
combination is deliberate: the database is where the interesting behaviour lives
(upserts, transaction boundaries, filtering), while the API is a boundary whose
contract is already pinned down by the client's own tests. Mocking the database
too would leave these tests asserting only that mocks were called.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.api.client import MetaMarketingClient
from app.api.schemas import (
    AdAccountPayload,
    AdCreativePayload,
    AdPayload,
    AdSetPayload,
    CampaignPayload,
    InsightsPayload,
)
from app.models.enums import (
    InsightLevel,
    RecommendationAction,
    RecommendationSeverity,
    RecommendationStatus,
)
from app.models.recommendation import Recommendation
from app.recommendations.engine import RecommendationEngine
from app.repositories.unit_of_work import UnitOfWorkFactory
from app.services.account_service import AccountService
from app.services.campaign_service import CampaignService
from app.services.creative_service import CreativeService
from app.services.insight_service import InsightService
from app.services.optimization_service import OptimizationService
from app.services.sync_service import SyncService
from app.utils.exceptions import OptimizationError, SynchronizationError
from tests.conftest import TEST_ACCOUNT_ID

pytestmark = pytest.mark.integration


def account_payload(currency: str = "USD") -> AdAccountPayload:
    """A minimal account payload."""
    return AdAccountPayload.model_validate(
        {
            "id": TEST_ACCOUNT_ID,
            "name": "Acme Ads",
            "currency": currency,
            "timezone_name": "America/New_York",
            "account_status": 1,
        }
    )


def campaign_payload(remote_id: str = "c1", daily_budget: str = "10000") -> CampaignPayload:
    """A minimal campaign payload with a budget in minor units."""
    return CampaignPayload.model_validate(
        {
            "id": remote_id,
            "name": f"Campaign {remote_id}",
            "status": "ACTIVE",
            "effective_status": "ACTIVE",
            "objective": "OUTCOME_SALES",
            "daily_budget": daily_budget,
        }
    )


def ad_set_payload(remote_id: str = "as1", campaign_id: str = "c1") -> AdSetPayload:
    """A minimal ad set payload."""
    return AdSetPayload.model_validate(
        {
            "id": remote_id,
            "campaign_id": campaign_id,
            "name": f"Ad set {remote_id}",
            "status": "ACTIVE",
            "daily_budget": "5000",
        }
    )


def creative_payload(remote_id: str = "cr1") -> AdCreativePayload:
    """A minimal creative payload."""
    return AdCreativePayload.model_validate(
        {"id": remote_id, "name": f"Creative {remote_id}", "body": "Buy our thing"}
    )


def ad_payload(
    remote_id: str = "a1",
    ad_set_id: str = "as1",
    creative_id: str | None = "cr1",
) -> AdPayload:
    """A minimal ad payload, optionally referencing a creative."""
    data: dict[str, object] = {
        "id": remote_id,
        "adset_id": ad_set_id,
        "name": f"Ad {remote_id}",
        "status": "ACTIVE",
    }
    if creative_id is not None:
        data["creative"] = {"id": creative_id}
    return AdPayload.model_validate(data)


def insights_payload(
    day: date,
    *,
    campaign_id: str = "c1",
    spend: str = "100.00",
    impressions: int = 10_000,
    clicks: int = 200,
    reach: int = 5_000,
    conversions: int = 10,
    conversion_value: str = "500.00",
) -> InsightsPayload:
    """A single-day campaign-level insights payload."""
    return InsightsPayload.model_validate(
        {
            "date_start": day.isoformat(),
            "date_stop": day.isoformat(),
            "campaign_id": campaign_id,
            "campaign_name": f"Campaign {campaign_id}",
            "spend": spend,
            "impressions": impressions,
            "clicks": clicks,
            "reach": reach,
            "actions": [{"action_type": "purchase", "value": str(conversions)}],
            "action_values": [{"action_type": "purchase", "value": conversion_value}],
        }
    )


@pytest.fixture
def fake_client() -> MagicMock:
    """A Meta client stub returning empty collections by default."""
    client = MagicMock(spec=MetaMarketingClient)
    client.list_ad_accounts.return_value = [account_payload()]
    client.get_ad_account.return_value = account_payload()
    client.list_campaigns.return_value = []
    client.list_ad_sets.return_value = []
    client.list_ad_creatives.return_value = []
    client.list_ads.return_value = []
    client.fetch_insights.return_value = []
    return client


@pytest.fixture
def sync_service(
    unit_of_work_factory: UnitOfWorkFactory,
    fake_client: MagicMock,
) -> SyncService:
    """A sync service wired to the test transaction and the client stub."""
    return SyncService(unit_of_work_factory=unit_of_work_factory, client=fake_client)


# ---------------------------------------------------------------------------
# SyncService
# ---------------------------------------------------------------------------


def test_sync_accounts_persists_every_reachable_account(
    sync_service: SyncService,
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    written = sync_service.sync_accounts()

    assert written == 1
    with unit_of_work_factory.start() as unit_of_work:
        assert unit_of_work.ad_accounts.get_by_remote_id(TEST_ACCOUNT_ID) is not None


def test_sync_structure_writes_the_whole_hierarchy(
    sync_service: SyncService,
    fake_client: MagicMock,
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    fake_client.list_campaigns.return_value = [campaign_payload()]
    fake_client.list_ad_sets.return_value = [ad_set_payload()]
    fake_client.list_ad_creatives.return_value = [creative_payload()]
    fake_client.list_ads.return_value = [ad_payload()]

    summary = sync_service.sync_structure(TEST_ACCOUNT_ID)

    assert (summary.campaigns, summary.ad_sets, summary.creatives, summary.ads) == (1, 1, 1, 1)
    with unit_of_work_factory.start() as unit_of_work:
        account = unit_of_work.ad_accounts.require_by_remote_id(TEST_ACCOUNT_ID)
        ads = unit_of_work.ads.list_for_account(account.id)
        assert len(ads) == 1
        assert ads[0].creative_id is not None


def test_sync_converts_budgets_using_the_account_currency(
    sync_service: SyncService,
    fake_client: MagicMock,
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    fake_client.list_campaigns.return_value = [campaign_payload(daily_budget="10000")]

    sync_service.sync_structure(TEST_ACCOUNT_ID)

    with unit_of_work_factory.start() as unit_of_work:
        campaign = unit_of_work.campaigns.get_by_remote_id("c1")
        assert campaign is not None
        assert campaign.daily_budget == Decimal("100.00")


def test_zero_decimal_currency_budgets_are_not_divided(
    sync_service: SyncService,
    fake_client: MagicMock,
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    fake_client.get_ad_account.return_value = account_payload(currency="JPY")
    fake_client.list_campaigns.return_value = [campaign_payload(daily_budget="10000")]

    sync_service.sync_structure(TEST_ACCOUNT_ID)

    with unit_of_work_factory.start() as unit_of_work:
        campaign = unit_of_work.campaigns.get_by_remote_id("c1")
        assert campaign is not None
        # ¥10,000 is ¥10,000, not ¥100.
        assert campaign.daily_budget == Decimal(10_000)


def test_orphaned_ad_sets_are_skipped_rather_than_misparented(
    sync_service: SyncService,
    fake_client: MagicMock,
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    fake_client.list_campaigns.return_value = [campaign_payload("c1")]
    fake_client.list_ad_sets.return_value = [ad_set_payload("as1", campaign_id="deleted")]

    summary = sync_service.sync_structure(TEST_ACCOUNT_ID)

    # Happens legitimately when a campaign is deleted between two requests.
    assert summary.ad_sets == 0
    with unit_of_work_factory.start() as unit_of_work:
        assert unit_of_work.ad_sets.get_by_remote_id("as1") is None


def test_ads_with_an_unresolved_creative_are_still_stored(
    sync_service: SyncService,
    fake_client: MagicMock,
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    fake_client.list_campaigns.return_value = [campaign_payload()]
    fake_client.list_ad_sets.return_value = [ad_set_payload()]
    fake_client.list_ads.return_value = [ad_payload(creative_id="missing")]

    summary = sync_service.sync_structure(TEST_ACCOUNT_ID)

    # The ad's performance history matters even without its creative.
    assert summary.ads == 1
    with unit_of_work_factory.start() as unit_of_work:
        stored = unit_of_work.ads.get_by_remote_id("a1")
        assert stored is not None
        assert stored.creative_id is None


def test_sync_structure_is_idempotent(
    sync_service: SyncService,
    fake_client: MagicMock,
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    fake_client.list_campaigns.return_value = [campaign_payload()]

    sync_service.sync_structure(TEST_ACCOUNT_ID)
    sync_service.sync_structure(TEST_ACCOUNT_ID)

    with unit_of_work_factory.start() as unit_of_work:
        account = unit_of_work.ad_accounts.require_by_remote_id(TEST_ACCOUNT_ID)
        assert unit_of_work.campaigns.count_for_account(account.id) == 1


def test_sync_insights_stores_rows_against_the_right_entity(
    sync_service: SyncService,
    fake_client: MagicMock,
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    sync_service.sync_accounts()
    fake_client.fetch_insights.return_value = [
        insights_payload(date(2026, 6, 1)),
        insights_payload(date(2026, 6, 2)),
    ]

    written = sync_service.sync_insights(
        TEST_ACCOUNT_ID,
        level=InsightLevel.CAMPAIGN,
        since=date(2026, 6, 1),
        until=date(2026, 6, 2),
    )

    assert written == 2
    with unit_of_work_factory.start() as unit_of_work:
        rows = unit_of_work.insights.list_for_entity(
            level=InsightLevel.CAMPAIGN,
            entity_remote_id="c1",
            since=date(2026, 6, 1),
            until=date(2026, 6, 2),
        )
        assert len(rows) == 2
        assert rows[0].conversions == 10
        assert rows[0].conversion_value == Decimal("500.00")


def test_resyncing_a_window_does_not_duplicate_rows(
    sync_service: SyncService,
    fake_client: MagicMock,
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    sync_service.sync_accounts()
    fake_client.fetch_insights.return_value = [insights_payload(date(2026, 6, 1))]

    sync_service.sync_insights(
        TEST_ACCOUNT_ID, level=InsightLevel.CAMPAIGN, since=date(2026, 6, 1), until=date(2026, 6, 1)
    )
    fake_client.fetch_insights.return_value = [insights_payload(date(2026, 6, 1), spend="250.00")]
    sync_service.sync_insights(
        TEST_ACCOUNT_ID, level=InsightLevel.CAMPAIGN, since=date(2026, 6, 1), until=date(2026, 6, 1)
    )

    with unit_of_work_factory.start() as unit_of_work:
        account = unit_of_work.ad_accounts.require_by_remote_id(TEST_ACCOUNT_ID)
        assert unit_of_work.insights.count_for_account(account.id) == 1
        rows = unit_of_work.insights.list_for_account(
            account.id,
            level=InsightLevel.CAMPAIGN,
            since=date(2026, 6, 1),
            until=date(2026, 6, 1),
        )
        assert rows[0].spend == Decimal("250.00")


def test_insights_row_without_an_identifier_is_rejected(
    sync_service: SyncService,
    fake_client: MagicMock,
) -> None:
    sync_service.sync_accounts()
    fake_client.fetch_insights.return_value = [
        InsightsPayload.model_validate(
            {"date_start": "2026-06-01", "date_stop": "2026-06-01", "spend": "10.00"}
        )
    ]

    # Storing it against a guessed entity would attribute one campaign's spend
    # to another, which is worse than failing.
    with pytest.raises(SynchronizationError):
        sync_service.sync_insights(
            TEST_ACCOUNT_ID,
            level=InsightLevel.CAMPAIGN,
            since=date(2026, 6, 1),
            until=date(2026, 6, 1),
        )


# ---------------------------------------------------------------------------
# Read services
# ---------------------------------------------------------------------------


def test_account_service_reads_locally_unless_asked_to_refresh(
    unit_of_work_factory: UnitOfWorkFactory,
    sync_service: SyncService,
    fake_client: MagicMock,
) -> None:
    service = AccountService(
        unit_of_work_factory=unit_of_work_factory,
        sync_service=sync_service,
    )

    assert service.list_accounts() == []
    fake_client.list_ad_accounts.assert_not_called()

    refreshed = service.list_accounts(refresh=True)

    assert len(refreshed) == 1
    fake_client.list_ad_accounts.assert_called_once()


def test_campaign_service_applies_its_filters(
    unit_of_work_factory: UnitOfWorkFactory,
    sync_service: SyncService,
    fake_client: MagicMock,
) -> None:
    fake_client.list_campaigns.return_value = [
        campaign_payload("c1"),
        campaign_payload("c2"),
    ]
    service = CampaignService(
        unit_of_work_factory=unit_of_work_factory,
        sync_service=sync_service,
    )

    all_campaigns = service.list_campaigns(TEST_ACCOUNT_ID, refresh=True)
    filtered = service.list_campaigns(TEST_ACCOUNT_ID, name_contains="c2")

    assert len(all_campaigns) == 2
    assert len(filtered) == 1
    assert service.count_campaigns(TEST_ACCOUNT_ID) == 2


def test_creative_service_counts_deployment(
    unit_of_work_factory: UnitOfWorkFactory,
    sync_service: SyncService,
    fake_client: MagicMock,
) -> None:
    fake_client.list_campaigns.return_value = [campaign_payload()]
    fake_client.list_ad_sets.return_value = [ad_set_payload()]
    fake_client.list_ad_creatives.return_value = [creative_payload("cr1"), creative_payload("cr2")]
    fake_client.list_ads.return_value = [
        ad_payload("a1", creative_id="cr1"),
        ad_payload("a2", creative_id="cr1"),
    ]
    service = CreativeService(
        unit_of_work_factory=unit_of_work_factory,
        sync_service=sync_service,
    )

    usages = service.list_creatives(TEST_ACCOUNT_ID, refresh=True)

    by_id = {usage.creative.remote_id: usage for usage in usages}
    assert by_id["cr1"].ad_count == 2
    assert by_id["cr1"].active_ad_count == 2
    assert by_id["cr2"].ad_count == 0
    assert by_id["cr2"].is_in_use is False
    # Most widely deployed first.
    assert usages[0].creative.remote_id == "cr1"


def test_creative_service_can_hide_retired_creatives(
    unit_of_work_factory: UnitOfWorkFactory,
    sync_service: SyncService,
    fake_client: MagicMock,
) -> None:
    fake_client.list_campaigns.return_value = [campaign_payload()]
    fake_client.list_ad_sets.return_value = [ad_set_payload()]
    fake_client.list_ad_creatives.return_value = [creative_payload("cr1"), creative_payload("cr2")]
    fake_client.list_ads.return_value = [ad_payload("a1", creative_id="cr1")]
    service = CreativeService(
        unit_of_work_factory=unit_of_work_factory,
        sync_service=sync_service,
    )

    in_use = service.list_creatives(TEST_ACCOUNT_ID, refresh=True, in_use_only=True)

    assert [usage.creative.remote_id for usage in in_use] == ["cr1"]


def test_insight_service_compares_against_the_preceding_window(
    unit_of_work_factory: UnitOfWorkFactory,
    sync_service: SyncService,
    fake_client: MagicMock,
) -> None:
    sync_service.sync_accounts()
    current_since, current_until = date(2026, 6, 8), date(2026, 6, 14)
    fake_client.fetch_insights.return_value = [
        # Previous window: cheaper.
        insights_payload(date(2026, 6, 2), spend="100.00", conversions=10),
        # Current window: twice the cost per result.
        insights_payload(date(2026, 6, 9), spend="200.00", conversions=10),
    ]
    service = InsightService(
        unit_of_work_factory=unit_of_work_factory,
        sync_service=sync_service,
    )

    report = service.performance_report(
        TEST_ACCOUNT_ID,
        level=InsightLevel.CAMPAIGN,
        since=current_since,
        until=current_until,
        refresh=True,
    )

    assert report.currency == "USD"
    entry = report.entries[0]
    assert entry.current.spend == Decimal("200.00")
    assert entry.previous.spend == Decimal("100.00")
    assert entry.comparison is not None
    assert entry.comparison.cost_per_acquisition.percent_change == Decimal(100)
    assert report.totals.spend == Decimal("200.00")


def test_refreshing_a_report_fetches_both_windows(
    unit_of_work_factory: UnitOfWorkFactory,
    sync_service: SyncService,
    fake_client: MagicMock,
) -> None:
    sync_service.sync_accounts()
    service = InsightService(
        unit_of_work_factory=unit_of_work_factory,
        sync_service=sync_service,
    )

    service.performance_report(
        TEST_ACCOUNT_ID,
        level=InsightLevel.CAMPAIGN,
        since=date(2026, 6, 8),
        until=date(2026, 6, 14),
        refresh=True,
    )

    # A comparison against absent history is no comparison at all.
    _, kwargs = fake_client.fetch_insights.call_args
    assert kwargs["since"] == date(2026, 6, 1)
    assert kwargs["until"] == date(2026, 6, 14)


# ---------------------------------------------------------------------------
# OptimizationService
# ---------------------------------------------------------------------------


@pytest.fixture
def optimization_setup(
    unit_of_work_factory: UnitOfWorkFactory,
    sync_service: SyncService,
    fake_client: MagicMock,
) -> OptimizationService:
    """An account with a wasteful campaign and the service to evaluate it."""
    fake_client.list_campaigns.return_value = [campaign_payload("c1")]
    sync_service.sync_structure(TEST_ACCOUNT_ID)
    fake_client.fetch_insights.return_value = [
        insights_payload(day, spend="200.00", conversions=0, conversion_value="0")
        for day in (date(2026, 6, 9) + timedelta(days=offset) for offset in range(3))
    ]
    insight_service = InsightService(
        unit_of_work_factory=unit_of_work_factory,
        sync_service=sync_service,
    )
    return OptimizationService(
        unit_of_work_factory=unit_of_work_factory,
        insight_service=insight_service,
        engine=RecommendationEngine(),
        client=fake_client,
    )


def test_generating_persists_findings(optimization_setup: OptimizationService) -> None:
    result = optimization_setup.generate_recommendations(
        TEST_ACCOUNT_ID,
        level=InsightLevel.CAMPAIGN,
        since=date(2026, 6, 8),
        until=date(2026, 6, 14),
        refresh=True,
    )

    assert result.entities_evaluated == 1
    assert result.recommendations
    codes = {recommendation.rule_code for recommendation in result.recommendations}
    assert "zero_conversion_spend" in codes


def test_regenerating_supersedes_rather_than_accumulates(
    optimization_setup: OptimizationService,
) -> None:
    for _ in range(2):
        optimization_setup.generate_recommendations(
            TEST_ACCOUNT_ID,
            level=InsightLevel.CAMPAIGN,
            since=date(2026, 6, 8),
            until=date(2026, 6, 14),
            refresh=True,
        )

    outstanding = optimization_setup.list_open_recommendations(TEST_ACCOUNT_ID)

    # One row per finding, not one row per time the command was run.
    by_rule = [recommendation.rule_code for recommendation in outstanding]
    assert len(by_rule) == len(set(by_rule))


def test_open_recommendations_lead_with_the_most_urgent(
    optimization_setup: OptimizationService,
) -> None:
    optimization_setup.generate_recommendations(
        TEST_ACCOUNT_ID,
        level=InsightLevel.CAMPAIGN,
        since=date(2026, 6, 8),
        until=date(2026, 6, 14),
        refresh=True,
    )

    outstanding = optimization_setup.list_open_recommendations(TEST_ACCOUNT_ID)

    ranks = [recommendation.severity.rank for recommendation in outstanding]
    assert ranks == sorted(ranks, reverse=True)


def test_applying_calls_meta_and_records_the_outcome(
    optimization_setup: OptimizationService,
    fake_client: MagicMock,
) -> None:
    result = optimization_setup.generate_recommendations(
        TEST_ACCOUNT_ID,
        level=InsightLevel.CAMPAIGN,
        since=date(2026, 6, 8),
        until=date(2026, 6, 14),
        refresh=True,
    )
    pausing = next(
        recommendation
        for recommendation in result.recommendations
        if recommendation.rule_code == "zero_conversion_spend"
    )

    applied = optimization_setup.apply_recommendation(pausing.id)

    fake_client.pause_entity.assert_called_once_with("c1")
    assert applied.status is RecommendationStatus.APPLIED
    assert applied.applied_at is not None


def test_applying_twice_is_refused(optimization_setup: OptimizationService) -> None:
    result = optimization_setup.generate_recommendations(
        TEST_ACCOUNT_ID,
        level=InsightLevel.CAMPAIGN,
        since=date(2026, 6, 8),
        until=date(2026, 6, 14),
        refresh=True,
    )
    target = result.recommendations[0]
    optimization_setup.apply_recommendation(target.id)

    with pytest.raises(OptimizationError, match="no longer open"):
        optimization_setup.apply_recommendation(target.id)


def test_dismissing_closes_a_recommendation_without_calling_meta(
    optimization_setup: OptimizationService,
    fake_client: MagicMock,
) -> None:
    result = optimization_setup.generate_recommendations(
        TEST_ACCOUNT_ID,
        level=InsightLevel.CAMPAIGN,
        since=date(2026, 6, 8),
        until=date(2026, 6, 14),
        refresh=True,
    )

    dismissed = optimization_setup.dismiss_recommendation(result.recommendations[0].id)

    assert dismissed.status is RecommendationStatus.DISMISSED
    fake_client.pause_entity.assert_not_called()
    fake_client.update_daily_budget.assert_not_called()


def test_budget_recommendations_convert_to_minor_units_when_applied(
    unit_of_work_factory: UnitOfWorkFactory,
    sync_service: SyncService,
    fake_client: MagicMock,
) -> None:
    fake_client.list_campaigns.return_value = [campaign_payload("c1", daily_budget="10000")]
    sync_service.sync_structure(TEST_ACCOUNT_ID)
    # Profitable and budget-bearing, so the scale-winner rule proposes +20%.
    fake_client.fetch_insights.return_value = [
        insights_payload(
            date(2026, 6, 9),
            spend="500.00",
            conversions=25,
            conversion_value="2000.00",
        )
    ]
    service = OptimizationService(
        unit_of_work_factory=unit_of_work_factory,
        insight_service=InsightService(
            unit_of_work_factory=unit_of_work_factory,
            sync_service=sync_service,
        ),
        engine=RecommendationEngine(),
        client=fake_client,
    )
    result = service.generate_recommendations(
        TEST_ACCOUNT_ID,
        level=InsightLevel.CAMPAIGN,
        since=date(2026, 6, 8),
        until=date(2026, 6, 14),
        refresh=True,
    )
    scaling = next(
        recommendation
        for recommendation in result.recommendations
        if recommendation.rule_code == "scale_winner"
    )

    service.apply_recommendation(scaling.id)

    # $120.00 major becomes 12000 minor; Meta rejects fractional budgets.
    fake_client.update_daily_budget.assert_called_once_with("c1", daily_budget_minor=12_000)


def test_applying_a_non_automatable_recommendation_is_refused(
    unit_of_work_factory: UnitOfWorkFactory,
    sync_service: SyncService,
    fake_client: MagicMock,
) -> None:
    sync_service.sync_accounts()
    engine = RecommendationEngine()
    service = OptimizationService(
        unit_of_work_factory=unit_of_work_factory,
        insight_service=InsightService(
            unit_of_work_factory=unit_of_work_factory,
            sync_service=sync_service,
        ),
        engine=engine,
        client=fake_client,
    )
    with unit_of_work_factory.start() as unit_of_work:
        account = unit_of_work.ad_accounts.require_by_remote_id(TEST_ACCOUNT_ID)
        advisory = unit_of_work.recommendations.add(
            Recommendation(
                ad_account_id=account.id,
                level=InsightLevel.CAMPAIGN,
                entity_remote_id="c1",
                entity_name="Campaign c1",
                rule_code="creative_fatigue",
                severity=RecommendationSeverity.WARNING,
                action=RecommendationAction.ROTATE_CREATIVE,
                title="Creative fatigue",
                rationale="Frequency is high and CTR is falling.",
                metric_snapshot={},
                suggested_change={},
                generated_at=datetime(2026, 6, 15, tzinfo=UTC),
            )
        )
        advisory_id = advisory.id

    # Rotating a creative needs judgement the engine does not have.
    with pytest.raises(OptimizationError, match="cannot be applied automatically"):
        service.apply_recommendation(advisory_id)
