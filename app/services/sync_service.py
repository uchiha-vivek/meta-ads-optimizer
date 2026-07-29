"""Synchronization of remote Meta state into the local database.

This is the only service that writes structural data. It fetches through
:class:`~app.api.client.MetaMarketingClient`, maps typed payloads onto ORM
models, and upserts them through repositories — the whole run inside one
transaction, so an API failure partway through leaves the database describing
the state before the sync rather than a half-updated account.

Ordering is dictated by foreign keys. An account must exist before its campaigns
can reference it, campaigns before ad sets, creatives before the ads that point
at them. Each stage builds a remote-ID-to-local-ID map for the next.

Money conversion happens here and only here. Payloads carry budgets in the
account currency's minor unit, and this is the first layer that knows what that
currency is, so it is the first layer able to convert correctly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from app.api.client import MetaMarketingClient
from app.api.schemas import (
    AdAccountPayload,
    AdCreativePayload,
    AdPayload,
    AdSetPayload,
    CampaignPayload,
    InsightsPayload,
)
from app.models.ad import Ad
from app.models.ad_account import AdAccount
from app.models.ad_creative import AdCreative
from app.models.ad_set import AdSet
from app.models.campaign import Campaign
from app.models.enums import EntityStatus, InsightLevel
from app.models.insight import InsightRecord
from app.repositories.unit_of_work import UnitOfWork, UnitOfWorkFactory
from app.utils.exceptions import SynchronizationError
from app.utils.money import minor_units_to_major

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SyncSummary:
    """Counts of what one synchronization run touched.

    Attributes:
        campaigns: Campaigns inserted or refreshed.
        ad_sets: Ad sets inserted or refreshed.
        ads: Ads inserted or refreshed.
        creatives: Creatives inserted or refreshed.
        insight_rows: Insight rows inserted or refreshed.
    """

    campaigns: int = 0
    ad_sets: int = 0
    ads: int = 0
    creatives: int = 0
    insight_rows: int = 0

    @property
    def total(self) -> int:
        """Total rows touched across every entity type."""
        return self.campaigns + self.ad_sets + self.ads + self.creatives + self.insight_rows


class SyncService:
    """Pulls an advertiser's Meta account into the local database."""

    def __init__(
        self,
        *,
        unit_of_work_factory: UnitOfWorkFactory,
        client: MetaMarketingClient,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._client = client

    def sync_accounts(self) -> int:
        """Refresh every ad account the token can reach.

        Returns:
            How many accounts were inserted or refreshed.

        Raises:
            MetaApiError: If the Graph API request fails.
            SynchronizationError: If the fetched data cannot be persisted.
        """
        payloads = self._client.list_ad_accounts()
        with self._unit_of_work_factory.start() as unit_of_work:
            for payload in payloads:
                unit_of_work.ad_accounts.upsert(_to_account_model(payload))
        _logger.info("Synchronized ad accounts", extra={"count": len(payloads)})
        return len(payloads)

    def sync_structure(self, account_remote_id: str) -> SyncSummary:
        """Refresh one account's campaigns, ad sets, creatives, and ads.

        The whole structure is written in one transaction. A partial structure —
        ad sets whose campaigns were not updated — would produce budget advice
        computed against stale parents.

        Args:
            account_remote_id: Meta account ID, including the ``act_`` prefix.

        Returns:
            Counts of what was written.

        Raises:
            MetaApiError: If any Graph API request fails.
            SynchronizationError: If the fetched data cannot be persisted.
        """
        account_payload = self._client.get_ad_account(account_remote_id)
        campaign_payloads = self._client.list_campaigns(account_remote_id)
        ad_set_payloads = self._client.list_ad_sets(account_remote_id)
        creative_payloads = self._client.list_ad_creatives(account_remote_id)
        ad_payloads = self._client.list_ads(account_remote_id)

        with self._unit_of_work_factory.start() as unit_of_work:
            account = unit_of_work.ad_accounts.upsert(_to_account_model(account_payload))
            currency = account.currency

            campaign_ids = self._write_campaigns(
                unit_of_work,
                campaign_payloads,
                account_id=account.id,
                currency=currency,
            )
            ad_set_ids = self._write_ad_sets(
                unit_of_work,
                ad_set_payloads,
                campaign_ids=campaign_ids,
                currency=currency,
            )
            creative_ids = self._write_creatives(
                unit_of_work,
                creative_payloads,
                account_id=account.id,
            )
            ad_count = self._write_ads(
                unit_of_work,
                ad_payloads,
                ad_set_ids=ad_set_ids,
                creative_ids=creative_ids,
            )

            summary = SyncSummary(
                campaigns=len(campaign_ids),
                ad_sets=len(ad_set_ids),
                ads=ad_count,
                creatives=len(creative_ids),
            )

        _logger.info(
            "Synchronized account structure",
            extra={
                "account_remote_id": account_remote_id,
                "campaigns": summary.campaigns,
                "ad_sets": summary.ad_sets,
                "ads": summary.ads,
                "creatives": summary.creatives,
            },
        )
        return summary

    def sync_insights(
        self,
        account_remote_id: str,
        *,
        level: InsightLevel,
        since: date,
        until: date,
    ) -> int:
        """Fetch and store daily insights for one account at one level.

        A single account-level request with a ``level`` parameter returns one row
        per entity per day. Requesting each campaign separately would be one
        request per campaign and would exhaust the rate limit budget on any large
        account.

        Args:
            account_remote_id: Meta account ID, including the ``act_`` prefix.
            level: Aggregation level to fetch.
            since: First day to fetch, inclusive.
            until: Last day to fetch, inclusive.

        Returns:
            How many rows were inserted or refreshed.

        Raises:
            MetaApiError: If the Graph API request fails.
            SynchronizationError: If a row cannot be attributed to an entity.
        """
        payloads = self._client.fetch_insights(
            entity_remote_id=account_remote_id,
            level=level.value,
            since=since,
            until=until,
        )

        with self._unit_of_work_factory.start() as unit_of_work:
            account = unit_of_work.ad_accounts.require_by_remote_id(account_remote_id)
            written = 0
            for payload in payloads:
                record = _to_insight_model(payload, level=level, ad_account_id=account.id)
                unit_of_work.insights.upsert(record)
                written += 1

        _logger.info(
            "Synchronized insights",
            extra={
                "account_remote_id": account_remote_id,
                "level": level.value,
                "since": since.isoformat(),
                "until": until.isoformat(),
                "rows": written,
            },
        )
        return written

    # -- stage helpers -----------------------------------------------------

    def _write_campaigns(
        self,
        unit_of_work: UnitOfWork,
        payloads: list[CampaignPayload],
        *,
        account_id: int,
        currency: str | None,
    ) -> dict[str, int]:
        """Upsert campaigns and map their Meta IDs to local primary keys."""
        campaign_ids: dict[str, int] = {}
        for payload in payloads:
            campaign = Campaign(
                remote_id=payload.remote_id,
                ad_account_id=account_id,
                name=payload.name,
                status=EntityStatus.from_meta(payload.status),
                effective_status=payload.effective_status,
                objective=payload.objective,
                buying_type=payload.buying_type,
                bid_strategy=payload.bid_strategy,
                daily_budget=minor_units_to_major(payload.daily_budget_minor, currency),
                lifetime_budget=minor_units_to_major(payload.lifetime_budget_minor, currency),
                start_time=payload.start_time,
                stop_time=payload.stop_time,
                created_time=payload.created_time,
            )
            stored = unit_of_work.campaigns.upsert(campaign)
            campaign_ids[stored.remote_id] = stored.id
        return campaign_ids

    def _write_ad_sets(
        self,
        unit_of_work: UnitOfWork,
        payloads: list[AdSetPayload],
        *,
        campaign_ids: dict[str, int],
        currency: str | None,
    ) -> dict[str, int]:
        """Upsert ad sets whose parent campaign is known.

        An ad set whose campaign was not returned is skipped rather than
        inserted against a guessed parent. This happens legitimately when a
        campaign is deleted between the two requests.
        """
        ad_set_ids: dict[str, int] = {}
        for payload in payloads:
            campaign_id = (
                campaign_ids.get(payload.campaign_remote_id)
                if payload.campaign_remote_id is not None
                else None
            )
            if campaign_id is None:
                _logger.warning(
                    "Skipping ad set whose parent campaign was not returned",
                    extra={
                        "ad_set_remote_id": payload.remote_id,
                        "campaign_remote_id": payload.campaign_remote_id,
                    },
                )
                continue

            ad_set = AdSet(
                remote_id=payload.remote_id,
                campaign_id=campaign_id,
                name=payload.name,
                status=EntityStatus.from_meta(payload.status),
                effective_status=payload.effective_status,
                optimization_goal=payload.optimization_goal,
                billing_event=payload.billing_event,
                daily_budget=minor_units_to_major(payload.daily_budget_minor, currency),
                lifetime_budget=minor_units_to_major(payload.lifetime_budget_minor, currency),
                bid_amount=minor_units_to_major(payload.bid_amount_minor, currency),
                start_time=payload.start_time,
                end_time=payload.end_time,
                created_time=payload.created_time,
            )
            stored = unit_of_work.ad_sets.upsert(ad_set)
            ad_set_ids[stored.remote_id] = stored.id
        return ad_set_ids

    def _write_creatives(
        self,
        unit_of_work: UnitOfWork,
        payloads: list[AdCreativePayload],
        *,
        account_id: int,
    ) -> dict[str, int]:
        """Upsert creatives and map their Meta IDs to local primary keys."""
        creative_ids: dict[str, int] = {}
        for payload in payloads:
            creative = AdCreative(
                remote_id=payload.remote_id,
                ad_account_id=account_id,
                name=payload.name,
                title=payload.title,
                body=payload.body,
                call_to_action_type=payload.call_to_action_type,
                object_type=payload.object_type,
                thumbnail_url=payload.thumbnail_url,
                image_url=payload.image_url,
                video_id=payload.video_id,
            )
            stored = unit_of_work.creatives.upsert(creative)
            creative_ids[stored.remote_id] = stored.id
        return creative_ids

    def _write_ads(
        self,
        unit_of_work: UnitOfWork,
        payloads: list[AdPayload],
        *,
        ad_set_ids: dict[str, int],
        creative_ids: dict[str, int],
    ) -> int:
        """Upsert ads whose parent ad set is known.

        An unresolved creative reference is stored as ``None`` rather than
        dropping the ad: the ad's performance history matters even when its
        creative was not returned.
        """
        written = 0
        for payload in payloads:
            ad_set_id = (
                ad_set_ids.get(payload.ad_set_remote_id)
                if payload.ad_set_remote_id is not None
                else None
            )
            if ad_set_id is None:
                _logger.warning(
                    "Skipping ad whose parent ad set was not returned",
                    extra={
                        "ad_remote_id": payload.remote_id,
                        "ad_set_remote_id": payload.ad_set_remote_id,
                    },
                )
                continue

            creative_id = (
                creative_ids.get(payload.creative_remote_id)
                if payload.creative_remote_id is not None
                else None
            )
            ad = Ad(
                remote_id=payload.remote_id,
                ad_set_id=ad_set_id,
                creative_id=creative_id,
                name=payload.name,
                status=EntityStatus.from_meta(payload.status),
                effective_status=payload.effective_status,
                created_time=payload.created_time,
            )
            unit_of_work.ads.upsert(ad)
            written += 1
        return written


def _to_account_model(payload: AdAccountPayload) -> AdAccount:
    """Map an account payload onto an ORM instance."""
    return AdAccount(
        remote_id=payload.remote_id,
        name=payload.name,
        business_name=payload.business_name,
        currency=payload.currency,
        timezone_name=payload.timezone_name,
        account_status=payload.account_status,
        spend_cap=minor_units_to_major(payload.spend_cap_minor, payload.currency),
        amount_spent=minor_units_to_major(payload.amount_spent_minor, payload.currency),
    )


def _to_insight_model(
    payload: InsightsPayload,
    *,
    level: InsightLevel,
    ad_account_id: int,
) -> InsightRecord:
    """Map an insights payload onto an ORM instance.

    Raises:
        SynchronizationError: If the row carries no identifier for the level it
            was requested at. Storing it against a guessed entity would silently
            attribute one campaign's spend to another.
    """
    entity_remote_id, entity_name = _identify_entity(payload, level)
    if entity_remote_id is None:
        raise SynchronizationError(
            "Insights row carries no identifier for the requested level",
            context={
                "level": level.value,
                "date_start": payload.date_start.isoformat(),
                "date_stop": payload.date_stop.isoformat(),
            },
        )

    return InsightRecord(
        ad_account_id=ad_account_id,
        level=level,
        entity_remote_id=entity_remote_id,
        entity_name=entity_name,
        date_start=payload.date_start,
        date_stop=payload.date_stop,
        spend=payload.spend,
        impressions=payload.impressions,
        clicks=payload.clicks,
        reach=payload.reach,
        conversions=payload.conversion_count(),
        conversion_value=payload.conversion_value(),
    )


def _identify_entity(
    payload: InsightsPayload,
    level: InsightLevel,
) -> tuple[str | None, str | None]:
    """Return the entity ID and name a row describes, given its level."""
    if level is InsightLevel.ACCOUNT:
        return payload.account_remote_id, None
    if level is InsightLevel.CAMPAIGN:
        return payload.campaign_remote_id, payload.campaign_name
    if level is InsightLevel.ADSET:
        return payload.ad_set_remote_id, payload.adset_name
    return payload.ad_remote_id, payload.ad_name
