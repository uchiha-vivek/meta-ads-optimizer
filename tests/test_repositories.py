"""Tests for the persistence layer, against real PostgreSQL.

These run against PostgreSQL rather than SQLite deliberately. A repository test
exists to prove *this* database accepts and answers the queries: the enum check
constraints, ``ILIKE`` matching, ``NULLS LAST`` ordering, and ``BIGINT`` columns
used here behave differently or not at all on another engine, so passing against
SQLite would prove nothing about production.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.models.ad_account import AdAccount
from app.models.enums import (
    EntityStatus,
    InsightLevel,
    RecommendationAction,
    RecommendationSeverity,
    RecommendationStatus,
)
from app.models.recommendation import Recommendation
from app.repositories.ad_account_repository import AdAccountRepository
from app.repositories.ad_creative_repository import AdCreativeRepository
from app.repositories.ad_repository import AdRepository
from app.repositories.ad_set_repository import AdSetRepository
from app.repositories.base import copy_scalar_columns
from app.repositories.campaign_repository import CampaignRepository
from app.repositories.insight_repository import InsightRepository
from app.repositories.recommendation_repository import RecommendationRepository
from app.utils.exceptions import EntityNotFoundError, RepositoryError
from tests.conftest import (
    build_account,
    build_ad,
    build_ad_set,
    build_campaign,
    build_creative,
    build_insight,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# AdAccountRepository
# ---------------------------------------------------------------------------


def test_account_upsert_inserts_then_updates_in_place(db_session: Session) -> None:
    repository = AdAccountRepository(db_session)

    inserted = repository.upsert(build_account(remote_id="act_1", name="Original"))
    updated = repository.upsert(build_account(remote_id="act_1", name="Renamed"))

    # Same row, refreshed: an upsert must not accumulate duplicates.
    assert inserted.id == updated.id
    assert updated.name == "Renamed"
    assert repository.count() == 1


def test_account_lookup_returns_none_when_never_synchronized(db_session: Session) -> None:
    repository = AdAccountRepository(db_session)

    assert repository.get_by_remote_id("act_missing") is None


def test_requiring_an_unknown_account_names_the_fix(db_session: Session) -> None:
    repository = AdAccountRepository(db_session)

    with pytest.raises(EntityNotFoundError) as failure:
        repository.require_by_remote_id("act_missing")

    # On a first run, "not found" means "not synced yet", not "does not exist".
    assert "--sync" in str(failure.value)


def test_accounts_are_listed_by_name(db_session: Session) -> None:
    repository = AdAccountRepository(db_session)
    repository.upsert(build_account(remote_id="act_z", name="Zebra"))
    repository.upsert(build_account(remote_id="act_a", name="Apple"))

    assert [account.name for account in repository.list_all()] == ["Apple", "Zebra"]


def test_require_by_id_raises_for_an_unknown_key(db_session: Session) -> None:
    repository = AdAccountRepository(db_session)

    with pytest.raises(EntityNotFoundError):
        repository.require_by_id(999_999)


# ---------------------------------------------------------------------------
# CampaignRepository
# ---------------------------------------------------------------------------


def test_campaign_upsert_refreshes_budget_and_status(
    db_session: Session,
    persisted_account: AdAccount,
) -> None:
    repository = CampaignRepository(db_session)
    repository.upsert(
        build_campaign(
            persisted_account.id,
            remote_id="c1",
            daily_budget=Decimal("100.00"),
            status=EntityStatus.ACTIVE,
        )
    )

    updated = repository.upsert(
        build_campaign(
            persisted_account.id,
            remote_id="c1",
            daily_budget=Decimal("250.00"),
            status=EntityStatus.PAUSED,
        )
    )

    assert updated.daily_budget == Decimal("250.00")
    assert updated.status is EntityStatus.PAUSED
    assert repository.count_for_account(persisted_account.id) == 1


def test_campaigns_can_be_filtered_by_status(
    db_session: Session,
    persisted_account: AdAccount,
) -> None:
    repository = CampaignRepository(db_session)
    repository.upsert(build_campaign(persisted_account.id, status=EntityStatus.ACTIVE))
    repository.upsert(build_campaign(persisted_account.id, status=EntityStatus.PAUSED))
    repository.upsert(build_campaign(persisted_account.id, status=EntityStatus.ARCHIVED))

    delivering = repository.list_delivering_for_account(persisted_account.id)

    assert len(delivering) == 1
    assert delivering[0].status is EntityStatus.ACTIVE


def test_campaign_name_filter_is_case_insensitive(
    db_session: Session,
    persisted_account: AdAccount,
) -> None:
    repository = CampaignRepository(db_session)
    repository.upsert(build_campaign(persisted_account.id, name="Spring Sale 2026"))
    repository.upsert(build_campaign(persisted_account.id, name="Winter Clearance"))

    matched = repository.list_for_account(persisted_account.id, name_contains="spring")

    assert len(matched) == 1
    assert matched[0].name == "Spring Sale 2026"


def test_campaigns_without_a_creation_time_sort_last(
    db_session: Session,
    persisted_account: AdAccount,
) -> None:
    repository = CampaignRepository(db_session)
    repository.upsert(build_campaign(persisted_account.id, name="Undated", created_time=None))
    repository.upsert(
        build_campaign(
            persisted_account.id,
            name="Recent",
            created_time=datetime(2026, 5, 1, tzinfo=UTC),
        )
    )

    # NULLS LAST is a PostgreSQL behaviour worth pinning down.
    assert [campaign.name for campaign in repository.list_for_account(persisted_account.id)] == [
        "Recent",
        "Undated",
    ]


def test_campaigns_are_scoped_to_their_account(db_session: Session) -> None:
    account_repository = AdAccountRepository(db_session)
    campaign_repository = CampaignRepository(db_session)
    first = account_repository.upsert(build_account())
    second = account_repository.upsert(build_account())
    campaign_repository.upsert(build_campaign(first.id))

    assert len(campaign_repository.list_for_account(first.id)) == 1
    assert campaign_repository.list_for_account(second.id) == []


# ---------------------------------------------------------------------------
# AdSetRepository and AdRepository
# ---------------------------------------------------------------------------


def test_ad_sets_are_reachable_through_their_campaign_and_account(
    db_session: Session,
    persisted_account: AdAccount,
) -> None:
    campaign = CampaignRepository(db_session).upsert(build_campaign(persisted_account.id))
    repository = AdSetRepository(db_session)
    repository.upsert(build_ad_set(campaign.id))
    repository.upsert(build_ad_set(campaign.id))

    assert len(repository.list_for_campaign(campaign.id)) == 2
    # The account-level query joins through campaigns rather than duplicating a
    # foreign key that could disagree with the campaign's own.
    assert len(repository.list_for_account(persisted_account.id)) == 2


def test_ads_can_be_found_by_the_creative_they_use(
    db_session: Session,
    persisted_account: AdAccount,
) -> None:
    campaign = CampaignRepository(db_session).upsert(build_campaign(persisted_account.id))
    ad_set = AdSetRepository(db_session).upsert(build_ad_set(campaign.id))
    creative = AdCreativeRepository(db_session).upsert(build_creative(persisted_account.id))
    repository = AdRepository(db_session)
    repository.upsert(build_ad(ad_set.id, creative_id=creative.id))
    repository.upsert(build_ad(ad_set.id, creative_id=creative.id))
    repository.upsert(build_ad(ad_set.id, creative_id=None))

    # The question creative fatigue asks: is this failing everywhere or here?
    assert len(repository.list_using_creative(creative.id)) == 2
    assert len(repository.list_for_account(persisted_account.id)) == 3


def test_creatives_are_listed_for_their_account(
    db_session: Session,
    persisted_account: AdAccount,
) -> None:
    repository = AdCreativeRepository(db_session)
    repository.upsert(build_creative(persisted_account.id, remote_id="cr1", name="Beta"))
    repository.upsert(build_creative(persisted_account.id, remote_id="cr2", name="Alpha"))

    assert [creative.name for creative in repository.list_for_account(persisted_account.id)] == [
        "Alpha",
        "Beta",
    ]


def test_creative_upsert_refreshes_copy(
    db_session: Session,
    persisted_account: AdAccount,
) -> None:
    repository = AdCreativeRepository(db_session)
    repository.upsert(build_creative(persisted_account.id, remote_id="cr1", body="Old copy"))

    updated = repository.upsert(
        build_creative(persisted_account.id, remote_id="cr1", body="New copy")
    )

    assert updated.body == "New copy"


# ---------------------------------------------------------------------------
# InsightRepository
# ---------------------------------------------------------------------------


def test_reinserting_a_window_updates_rather_than_duplicates(
    db_session: Session,
    persisted_account: AdAccount,
) -> None:
    repository = InsightRepository(db_session)
    day = date(2026, 6, 1)
    repository.upsert(
        build_insight(persisted_account.id, entity_remote_id="c1", day=day, spend=Decimal("100.00"))
    )

    updated = repository.upsert(
        build_insight(persisted_account.id, entity_remote_id="c1", day=day, spend=Decimal("175.00"))
    )

    # Meta restates recent days as attribution windows close; inserting instead
    # would double every aggregate computed over that period.
    assert updated.spend == Decimal("175.00")
    assert repository.count_for_account(persisted_account.id) == 1


def test_insight_rows_are_returned_in_chronological_order(
    db_session: Session,
    persisted_account: AdAccount,
) -> None:
    repository = InsightRepository(db_session)
    for offset in (2, 0, 1):
        repository.upsert(
            build_insight(
                persisted_account.id,
                entity_remote_id="c1",
                day=date(2026, 6, 1) + timedelta(days=offset),
            )
        )

    rows = repository.list_for_entity(
        level=InsightLevel.CAMPAIGN,
        entity_remote_id="c1",
        since=date(2026, 6, 1),
        until=date(2026, 6, 30),
    )

    # Every trend calculation depends on this ordering.
    assert [row.date_start.day for row in rows] == [1, 2, 3]


def test_insight_rows_outside_the_range_are_excluded(
    db_session: Session,
    persisted_account: AdAccount,
) -> None:
    repository = InsightRepository(db_session)
    repository.upsert(
        build_insight(persisted_account.id, entity_remote_id="c1", day=date(2026, 5, 1))
    )
    repository.upsert(
        build_insight(persisted_account.id, entity_remote_id="c1", day=date(2026, 6, 10))
    )

    rows = repository.list_for_account(
        persisted_account.id,
        level=InsightLevel.CAMPAIGN,
        since=date(2026, 6, 1),
        until=date(2026, 6, 30),
    )

    assert len(rows) == 1


def test_levels_do_not_bleed_into_each_other(
    db_session: Session,
    persisted_account: AdAccount,
) -> None:
    repository = InsightRepository(db_session)
    day = date(2026, 6, 1)
    repository.upsert(
        build_insight(
            persisted_account.id, entity_remote_id="e1", day=day, level=InsightLevel.CAMPAIGN
        )
    )
    repository.upsert(
        build_insight(
            persisted_account.id, entity_remote_id="e1", day=day, level=InsightLevel.ADSET
        )
    )

    # Same entity ID at two levels is legitimate and must stay distinct.
    campaign_rows = repository.list_for_account(
        persisted_account.id, level=InsightLevel.CAMPAIGN, since=day, until=day
    )
    assert len(campaign_rows) == 1
    assert repository.count_for_account(persisted_account.id) == 2


def test_latest_date_is_none_before_anything_is_stored(
    db_session: Session,
    persisted_account: AdAccount,
) -> None:
    repository = InsightRepository(db_session)

    assert (
        repository.latest_date_for_account(persisted_account.id, level=InsightLevel.CAMPAIGN)
        is None
    )


def test_latest_date_reports_the_most_recent_window(
    db_session: Session,
    persisted_account: AdAccount,
) -> None:
    repository = InsightRepository(db_session)
    repository.upsert(
        build_insight(persisted_account.id, entity_remote_id="c1", day=date(2026, 6, 1))
    )
    repository.upsert(
        build_insight(persisted_account.id, entity_remote_id="c1", day=date(2026, 6, 9))
    )

    latest = repository.latest_date_for_account(persisted_account.id, level=InsightLevel.CAMPAIGN)

    assert latest == date(2026, 6, 9)


# ---------------------------------------------------------------------------
# RecommendationRepository
# ---------------------------------------------------------------------------


def make_recommendation(ad_account_id: int, **overrides: object) -> Recommendation:
    """Build an unsaved recommendation with sensible defaults."""
    values: dict[str, object] = {
        "ad_account_id": ad_account_id,
        "level": InsightLevel.CAMPAIGN,
        "entity_remote_id": "c1",
        "entity_name": "Spring Sale",
        "rule_code": "zero_conversion_spend",
        "severity": RecommendationSeverity.CRITICAL,
        "action": RecommendationAction.PAUSE_ENTITY,
        "title": "Spent with no conversions",
        "rationale": "Detailed reasoning.",
        "metric_snapshot": {"spend": "500.00"},
        "suggested_change": {"field": "status", "proposed_value": "paused"},
        "generated_at": datetime(2026, 6, 15, tzinfo=UTC),
    }
    values.update(overrides)
    return Recommendation(**values)


def test_recommendations_round_trip_their_json_columns(
    db_session: Session,
    persisted_account: AdAccount,
) -> None:
    repository = RecommendationRepository(db_session)

    stored = repository.add(make_recommendation(persisted_account.id))
    db_session.flush()

    assert stored.metric_snapshot == {"spend": "500.00"}
    assert stored.suggested_change["proposed_value"] == "paused"
    assert stored.is_open is True


def test_only_open_recommendations_are_listed(
    db_session: Session,
    persisted_account: AdAccount,
) -> None:
    repository = RecommendationRepository(db_session)
    repository.add(make_recommendation(persisted_account.id))
    repository.add(make_recommendation(persisted_account.id, status=RecommendationStatus.DISMISSED))

    assert len(repository.list_open_for_account(persisted_account.id)) == 1
    assert repository.count_open_for_account(persisted_account.id) == 1


def test_superseding_closes_the_previous_generation(
    db_session: Session,
    persisted_account: AdAccount,
) -> None:
    repository = RecommendationRepository(db_session)
    repository.add(make_recommendation(persisted_account.id))
    repository.add(make_recommendation(persisted_account.id))

    superseded = repository.supersede_open_for_rule(
        entity_remote_id="c1",
        rule_code="zero_conversion_spend",
    )

    # Otherwise re-running the command accumulates one row per invocation.
    assert superseded == 2
    assert repository.count_open_for_account(persisted_account.id) == 0


def test_superseding_leaves_other_rules_untouched(
    db_session: Session,
    persisted_account: AdAccount,
) -> None:
    repository = RecommendationRepository(db_session)
    repository.add(make_recommendation(persisted_account.id, rule_code="zero_conversion_spend"))
    repository.add(make_recommendation(persisted_account.id, rule_code="creative_fatigue"))

    repository.supersede_open_for_rule(
        entity_remote_id="c1",
        rule_code="zero_conversion_spend",
    )

    assert repository.count_open_for_account(persisted_account.id) == 1


def test_finding_an_open_recommendation_for_a_rule(
    db_session: Session,
    persisted_account: AdAccount,
) -> None:
    repository = RecommendationRepository(db_session)
    repository.add(make_recommendation(persisted_account.id))

    found = repository.find_open_for_rule(
        entity_remote_id="c1",
        rule_code="zero_conversion_spend",
    )

    assert found is not None
    assert repository.find_open_for_rule(entity_remote_id="c1", rule_code="other") is None


def test_applying_and_dismissing_record_their_outcome(
    db_session: Session,
    persisted_account: AdAccount,
) -> None:
    repository = RecommendationRepository(db_session)
    applied = repository.add(make_recommendation(persisted_account.id))
    dismissed = repository.add(make_recommendation(persisted_account.id))
    applied_at = datetime(2026, 6, 16, 12, tzinfo=UTC)

    repository.mark_applied(applied, applied_at=applied_at)
    repository.mark_dismissed(dismissed)

    assert applied.status is RecommendationStatus.APPLIED
    assert applied.applied_at == applied_at
    assert dismissed.status is RecommendationStatus.DISMISSED
    assert repository.count_open_for_account(persisted_account.id) == 0


# ---------------------------------------------------------------------------
# Shared primitives
# ---------------------------------------------------------------------------


def test_copying_columns_between_different_models_is_refused(
    persisted_account: AdAccount,
) -> None:
    campaign = build_campaign(persisted_account.id)

    with pytest.raises(RepositoryError):
        copy_scalar_columns(source=persisted_account, target=campaign)


def test_copying_preserves_the_primary_key_of_the_stored_row() -> None:
    stored = build_account(remote_id="act_1", name="Stored")
    stored.id = 42
    incoming = build_account(remote_id="act_1", name="Incoming")

    copy_scalar_columns(source=incoming, target=stored)

    # The primary key identifies the stored row, not the incoming one.
    assert stored.id == 42
    assert stored.name == "Incoming"


def test_constraint_violations_surface_as_repository_errors(
    db_session: Session,
    persisted_account: AdAccount,
) -> None:
    repository = CampaignRepository(db_session)
    repository.upsert(build_campaign(persisted_account.id, remote_id="duplicate"))

    # Staged directly rather than through upsert, so the unique violation is
    # forced on the next flush and we can prove the SQLAlchemy exception is
    # translated rather than escaping the layer.
    db_session.add(build_campaign(persisted_account.id, remote_id="duplicate"))

    with pytest.raises(RepositoryError):
        repository.upsert(build_campaign(persisted_account.id, remote_id="another"))
