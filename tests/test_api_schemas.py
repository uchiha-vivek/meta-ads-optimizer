"""Tests for validating Graph API payloads into typed models."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.api.schemas import (
    AdAccountPayload,
    AdCreativePayload,
    AdPayload,
    AdSetPayload,
    CampaignPayload,
    InsightsPayload,
)


def test_account_payload_maps_meta_field_names() -> None:
    payload = AdAccountPayload.model_validate(
        {
            "id": "act_123",
            "name": "Acme",
            "currency": "USD",
            "timezone_name": "America/New_York",
            "account_status": 1,
            "spend_cap": "500000",
            "amount_spent": "125050",
        }
    )

    assert payload.remote_id == "act_123"
    # Budgets keep Meta's minor units here; conversion needs a currency the
    # payload does not carry, so it happens in the sync service.
    assert payload.spend_cap_minor == 500_000
    assert payload.amount_spent_minor == 125_050


def test_unknown_fields_are_ignored() -> None:
    # Meta adds response fields without notice; strictness would reject a
    # payload that is perfectly usable.
    payload = CampaignPayload.model_validate(
        {"id": "c1", "name": "Spring", "some_field_invented_next_quarter": 42}
    )

    assert payload.remote_id == "c1"


def test_campaign_budgets_and_timestamps_are_parsed() -> None:
    payload = CampaignPayload.model_validate(
        {
            "id": "c1",
            "status": "ACTIVE",
            "daily_budget": "10000",
            "created_time": "2026-01-15T10:30:00+0000",
        }
    )

    assert payload.daily_budget_minor == 10_000
    assert payload.lifetime_budget_minor is None
    assert payload.created_time is not None
    assert payload.created_time.year == 2026


def test_ad_set_links_to_its_campaign() -> None:
    payload = AdSetPayload.model_validate({"id": "as1", "campaign_id": "c1", "bid_amount": "250"})

    assert payload.campaign_remote_id == "c1"
    assert payload.bid_amount_minor == 250


def test_ad_payload_flattens_the_nested_creative_reference() -> None:
    payload = AdPayload.model_validate({"id": "a1", "adset_id": "as1", "creative": {"id": "cr1"}})

    assert payload.creative_remote_id == "cr1"
    assert payload.ad_set_remote_id == "as1"


def test_ad_payload_tolerates_a_missing_creative() -> None:
    payload = AdPayload.model_validate({"id": "a1", "adset_id": "as1"})

    assert payload.creative_remote_id is None


def test_creative_payload_maps_content_fields() -> None:
    payload = AdCreativePayload.model_validate(
        {"id": "cr1", "title": "Buy now", "body": "Great value", "video_id": "v9"}
    )

    assert payload.title == "Buy now"
    assert payload.video_id == "v9"


def test_insights_spend_is_already_in_major_units() -> None:
    payload = InsightsPayload.model_validate(
        {
            "date_start": "2026-06-01",
            "date_stop": "2026-06-01",
            "spend": "123.45",
            "impressions": "10000",
            "clicks": "250",
            "reach": "8000",
        }
    )

    assert payload.spend == Decimal("123.45")
    assert payload.date_start == date(2026, 6, 1)
    assert payload.impressions == 10_000


def test_conversions_use_the_first_priority_action_type_not_the_sum() -> None:
    """Meta reports one purchase under several overlapping action types.

    Summing them would turn a single sale into three, inflating conversions and
    deflating cost per acquisition by the same factor.
    """
    payload = InsightsPayload.model_validate(
        {
            "date_start": "2026-06-01",
            "date_stop": "2026-06-01",
            "actions": [
                {"action_type": "landing_page_view", "value": "80"},
                {"action_type": "omni_purchase", "value": "12"},
                {"action_type": "purchase", "value": "12"},
                {"action_type": "offsite_conversion.fb_pixel_purchase", "value": "12"},
            ],
            "action_values": [
                {"action_type": "purchase", "value": "1500.50"},
                {"action_type": "omni_purchase", "value": "1500.50"},
            ],
        }
    )

    assert payload.conversion_count() == 12
    assert payload.conversion_value() == Decimal("1500.50")


def test_conversions_fall_back_through_the_priority_list() -> None:
    payload = InsightsPayload.model_validate(
        {
            "date_start": "2026-06-01",
            "date_stop": "2026-06-01",
            "actions": [{"action_type": "omni_purchase", "value": "7"}],
        }
    )

    assert payload.conversion_count() == 7


def test_absent_conversion_actions_yield_zero() -> None:
    payload = InsightsPayload.model_validate(
        {
            "date_start": "2026-06-01",
            "date_stop": "2026-06-01",
            "actions": [{"action_type": "page_engagement", "value": "40"}],
        }
    )

    assert payload.conversion_count() == 0
    assert payload.conversion_value() == Decimal(0)


def test_missing_metrics_default_to_zero() -> None:
    payload = InsightsPayload.model_validate(
        {"date_start": "2026-06-01", "date_stop": "2026-06-01"}
    )

    assert payload.spend == Decimal(0)
    assert payload.impressions == 0
    assert payload.actions == []
