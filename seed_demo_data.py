"""Seed the local database with realistic demo data for a walkthrough.

This inserts data in the exact shape ``meta ... --sync`` would have produced, so
every read command works without a live Meta token:

    meta accounts      -> one account
    meta campaigns     -> seven campaigns
    meta insights      -> per-campaign performance, this week vs last week
    meta optimize      -> six recommendations, one per built-in rule
    meta creatives     -> a small creative library with deployment counts

Six campaigns are tuned so that each triggers exactly one rule; a seventh is
healthy and produces no finding, for contrast.

Run inside the app container:

    docker compose run --rm app python /workspace/seed_demo_data.py

It is idempotent: it deletes any existing account with the same remote id
(cascading to its campaigns, ad sets, ads, creatives, insights, and
recommendations) and rebuilds from scratch. Delete this file whenever you like;
it is only a demo convenience.

IMPORTANT: the numbers here are fabricated for demonstration. They are not real
advertising data.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from app.models.ad import Ad
from app.models.ad_account import AdAccount
from app.models.ad_creative import AdCreative
from app.models.ad_set import AdSet
from app.models.campaign import Campaign
from app.models.enums import EntityStatus, InsightLevel
from app.models.insight import InsightRecord

# --- date windows -----------------------------------------------------------
# Computed from the same clock the CLI uses (datetime.now(UTC)), so the default
# 7-day window (which ends yesterday) splits exactly at the boundary between the
# "previous" and "current" insight rows below. Run the demo commands with the
# default window (no --days, or --days 7).
_TODAY: date = datetime.now(UTC).date()
_CURR_UNTIL: date = _TODAY - timedelta(days=1)          # yesterday
_CURR_SINCE: date = _CURR_UNTIL - timedelta(days=6)     # 7-day current window
_PREV_UNTIL: date = _CURR_SINCE - timedelta(days=1)
_PREV_SINCE: date = _PREV_UNTIL - timedelta(days=6)     # 7-day previous window


def _totals(
    *,
    spend: str,
    impressions: int,
    clicks: int,
    reach: int,
    conversions: int,
    conversion_value: str,
) -> dict[str, object]:
    """A 7-day block of measured quantities for one campaign."""
    return {
        "spend": Decimal(spend),
        "impressions": impressions,
        "clicks": clicks,
        "reach": reach,
        "conversions": conversions,
        "conversion_value": Decimal(conversion_value),
    }


# --- the campaigns ----------------------------------------------------------
# Each tuple: (name, remote_id, daily_budget, previous 7 days, current 7 days,
#              which rule it is designed to demonstrate).
# daily_budget is None when the budget lives on the ad set rather than here.
_CAMPAIGNS: list[dict[str, object]] = [
    {
        "name": "Retargeting — Dynamic Product Ads",
        "remote_id": "23851000000000001",
        "daily_budget": Decimal("25"),
        "rule": "zero_conversion_spend",
        "previous": _totals(
            spend="130", impressions=21000, clicks=600, reach=9000,
            conversions=0, conversion_value="0",
        ),
        "current": _totals(
            spend="140", impressions=21000, clicks=420, reach=9000,
            conversions=0, conversion_value="0",
        ),
    },
    {
        "name": "Winter Coats — Conversions",
        "remote_id": "23851000000000002",
        "daily_budget": Decimal("55"),
        "rule": "rising_cost_per_acquisition",
        "previous": _totals(
            spend="210", impressions=28000, clicks=840, reach=12000,
            conversions=21, conversion_value="600",
        ),
        "current": _totals(
            spend="280", impressions=30000, clicks=700, reach=13000,
            conversions=8, conversion_value="350",
        ),
    },
    {
        "name": "Summer Sale — Prospecting",
        "remote_id": "23851000000000003",
        "daily_budget": None,
        "rule": "creative_fatigue",
        "previous": _totals(
            spend="600", impressions=40000, clicks=800, reach=12000,
            conversions=30, conversion_value="1000",
        ),
        "current": _totals(
            spend="650", impressions=50000, clicks=500, reach=15000,
            conversions=30, conversion_value="1050",
        ),
    },
    {
        "name": "Broad Awareness — Reach",
        "remote_id": "23851000000000004",
        "daily_budget": None,
        "rule": "low_click_through_rate",
        "previous": _totals(
            spend="290", impressions=78000, clicks=250, reach=58000,
            conversions=6, conversion_value="190",
        ),
        "current": _totals(
            spend="300", impressions=80000, clicks=240, reach=60000,
            conversions=6, conversion_value="200",
        ),
    },
    {
        "name": "Best Sellers — Winner",
        "remote_id": "23851000000000005",
        "daily_budget": Decimal("100"),
        "rule": "scale_winner",
        "previous": _totals(
            spend="480", impressions=29000, clicks=870, reach=11500,
            conversions=38, conversion_value="2400",
        ),
        "current": _totals(
            spend="500", impressions=30000, clicks=900, reach=12000,
            conversions=40, conversion_value="2500",
        ),
    },
    {
        "name": "Narrow Audience — Lead Gen",
        "remote_id": "23851000000000006",
        "daily_budget": Decimal("100"),
        "rule": "budget_underspend",
        "previous": _totals(
            spend="205", impressions=5900, clicks=118, reach=2950,
            conversions=8, conversion_value="295",
        ),
        "current": _totals(
            spend="210", impressions=6000, clicks=120, reach=3000,
            conversions=8, conversion_value="300",
        ),
    },
    {
        "name": "Evergreen Brand — Always On",
        "remote_id": "23851000000000007",
        "daily_budget": Decimal("80"),
        "rule": "(healthy — no finding)",
        "previous": _totals(
            spend="410", impressions=24500, clicks=740, reach=13800,
            conversions=24, conversion_value="770",
        ),
        "current": _totals(
            spend="420", impressions=25000, clicks=750, reach=14000,
            conversions=25, conversion_value="780",
        ),
    },
]

# --- the creative library ---------------------------------------------------
# (remote_id, name, title, body, cta, object_type, video?)
_CREATIVES: list[dict[str, object]] = [
    {
        "remote_id": "6251000000000001",
        "name": "Summer Hero Video 15s",
        "title": "The Summer Sale Is On",
        "body": "Up to 40% off everything. This week only.",
        "cta": "SHOP_NOW",
        "object_type": "VIDEO",
        "video": True,
    },
    {
        "remote_id": "6251000000000002",
        "name": "Winter Coat Carousel",
        "title": "Stay Warm in Style",
        "body": "Our best-reviewed coats, now in six new colours.",
        "cta": "SHOP_NOW",
        "object_type": "SHARE",
        "video": False,
    },
    {
        "remote_id": "6251000000000003",
        "name": "Brand Awareness Banner",
        "title": "Made for Everyday",
        "body": "Discover the brand thousands trust.",
        "cta": "LEARN_MORE",
        "object_type": "PHOTO",
        "video": False,
    },
    {
        "remote_id": "6251000000000004",
        "name": "Retargeting Dynamic Template",
        "title": "Still thinking it over?",
        "body": "Your picks are waiting — free returns on every order.",
        "cta": "SHOP_NOW",
        "object_type": "TEMPLATE",
        "video": False,
    },
    {
        "remote_id": "6251000000000005",
        "name": "Spring Promo (retired)",
        "title": "Spring Into Savings",
        "body": "Last season's campaign creative, no longer running.",
        "cta": "SHOP_NOW",
        "object_type": "PHOTO",
        "video": False,
    },
]

# Which creative each campaign's ad uses, and whether that ad is delivering.
# Creative index is 0-based into _CREATIVES. Creative 4 ("Spring Promo") is
# referenced by no ad, so it appears only without --in-use.
#   campaign_remote_id -> (creative_index, ad_status)
_AD_WIRING: dict[str, tuple[int, EntityStatus]] = {
    "23851000000000001": (3, EntityStatus.ACTIVE),   # Retargeting  -> Dynamic Template
    "23851000000000002": (1, EntityStatus.ACTIVE),   # Winter Coats -> Coat Carousel
    "23851000000000003": (0, EntityStatus.ACTIVE),   # Summer Sale  -> Hero Video
    "23851000000000004": (2, EntityStatus.ACTIVE),   # Broad Aware  -> Banner
    "23851000000000005": (0, EntityStatus.ACTIVE),   # Best Sellers -> Hero Video
    "23851000000000006": (2, EntityStatus.ACTIVE),   # Narrow Aud   -> Banner
    "23851000000000007": (0, EntityStatus.PAUSED),   # Evergreen    -> Hero Video (paused)
}


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL is not set; run this inside the app container.")
    return url


def _account_remote_id() -> str:
    account_id = os.environ.get("META_AD_ACCOUNT_ID")
    if not account_id:
        raise SystemExit("META_AD_ACCOUNT_ID is not set; check your .env.")
    return account_id


def _insight_rows(account_id: int, campaign: dict[str, object]) -> list[InsightRecord]:
    """Two campaign-level rows: the previous window and the current window."""
    common = {
        "ad_account_id": account_id,
        "level": InsightLevel.CAMPAIGN,
        "entity_remote_id": campaign["remote_id"],
        "entity_name": campaign["name"],
    }
    previous = campaign["previous"]
    current = campaign["current"]
    return [
        InsightRecord(
            **common,
            date_start=_PREV_SINCE,
            date_stop=_PREV_UNTIL,
            **previous,  # type: ignore[arg-type]
        ),
        InsightRecord(
            **common,
            date_start=_CURR_SINCE,
            date_stop=_CURR_UNTIL,
            **current,  # type: ignore[arg-type]
        ),
    ]


def seed() -> None:
    account_remote_id = _account_remote_id()
    engine = create_engine(_database_url())

    with Session(engine) as session:
        # Idempotency: drop any prior account with this remote id. The cascade
        # rules on the ORM relationships remove campaigns, ad sets, ads, and
        # creatives; insights and recommendations are cleared explicitly because
        # they carry no ORM-level relationship back from those children.
        existing = session.scalar(
            select(AdAccount).where(AdAccount.remote_id == account_remote_id)
        )
        if existing is not None:
            session.execute(
                delete(InsightRecord).where(InsightRecord.ad_account_id == existing.id)
            )
            session.delete(existing)
            session.flush()

        account = AdAccount(
            remote_id=account_remote_id,
            name="Demo Retail Co.",
            business_name="Demo Retail Co.",
            currency="USD",
            timezone_name="America/Los_Angeles",
            account_status=1,
            spend_cap=Decimal("100000"),
            amount_spent=Decimal("48210"),
        )
        session.add(account)
        session.flush()  # assign account.id

        # Creatives (owned by the account).
        creatives: list[AdCreative] = []
        for spec in _CREATIVES:
            creative = AdCreative(
                ad_account_id=account.id,
                remote_id=spec["remote_id"],
                name=spec["name"],
                title=spec["title"],
                body=spec["body"],
                call_to_action_type=spec["cta"],
                object_type=spec["object_type"],
                video_id=("v" + str(spec["remote_id"])) if spec["video"] else None,
            )
            session.add(creative)
            creatives.append(creative)
        session.flush()  # assign creative ids

        # Campaigns, each with one ad set and one ad wired to a creative.
        created_at = datetime.now(UTC) - timedelta(days=90)
        for spec in _CAMPAIGNS:
            campaign = Campaign(
                ad_account_id=account.id,
                remote_id=spec["remote_id"],
                name=spec["name"],
                status=EntityStatus.ACTIVE,
                effective_status="ACTIVE",
                objective="OUTCOME_SALES",
                buying_type="AUCTION",
                bid_strategy="LOWEST_COST_WITHOUT_CAP",
                daily_budget=spec["daily_budget"],
                created_time=created_at,
            )
            session.add(campaign)
            session.flush()  # assign campaign.id

            # If the budget lives on the campaign, the ad set carries none;
            # otherwise the ad set holds it (Summer Sale, Broad Awareness).
            adset_budget = None if spec["daily_budget"] is not None else Decimal("75")
            ad_set = AdSet(
                campaign_id=campaign.id,
                remote_id=str(spec["remote_id"]) + "01",
                name=str(spec["name"]) + " — Ad Set",
                status=EntityStatus.ACTIVE,
                effective_status="ACTIVE",
                optimization_goal="OFFSITE_CONVERSIONS",
                billing_event="IMPRESSIONS",
                daily_budget=adset_budget,
                created_time=created_at,
            )
            session.add(ad_set)
            session.flush()  # assign ad_set.id

            creative_index, ad_status = _AD_WIRING[str(spec["remote_id"])]
            session.add(
                Ad(
                    ad_set_id=ad_set.id,
                    creative_id=creatives[creative_index].id,
                    remote_id=str(spec["remote_id"]) + "001",
                    name=str(spec["name"]) + " — Ad",
                    status=ad_status,
                    effective_status=ad_status.value.upper(),
                    created_time=created_at,
                )
            )

            for row in _insight_rows(account.id, spec):
                session.add(row)

        session.commit()

    print("Seeded demo data:")
    print(f"  account         : {account_remote_id} (USD)")
    print(f"  campaigns       : {len(_CAMPAIGNS)}")
    print(f"  creatives       : {len(_CREATIVES)}")
    print(f"  current window  : {_CURR_SINCE.isoformat()} .. {_CURR_UNTIL.isoformat()}")
    print(f"  previous window : {_PREV_SINCE.isoformat()} .. {_PREV_UNTIL.isoformat()}")
    print()
    print("Now try (use the default 7-day window):")
    print("  docker compose run --rm app meta campaigns")
    print("  docker compose run --rm app meta insights")
    print("  docker compose run --rm app meta optimize --detail")
    print("  docker compose run --rm app meta creatives")


if __name__ == "__main__":
    seed()
